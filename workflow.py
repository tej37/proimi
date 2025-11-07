"""
LangGraph Orchestrator Workflow for Proimi Home Multi-Agent System
Routes customer messages to specialized agents and manages conversation flow
"""

import logging
from typing import Annotated, TypedDict, Literal
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langchain_cohere import ChatCohere
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from agents import get_airtable_agent, get_email_agent, initialize_agents
from prompts import ORCHESTRATOR_PROMPT, AIRTABLE_AGENT_PROMPT, EMAIL_AGENT_PROMPT

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cohere configuration
COHERE_API_KEY = "O74GRFs50FXvvoHn7Y9SyhX7s13YqJtqEsvJ4IAt"
COHERE_MODEL = "command-a-03-2025"


# ============================================================================
# State Definition
# ============================================================================

class OrchestratorState(TypedDict):
    """State for the orchestrator workflow"""
    messages: Annotated[list, add_messages]  # Conversation history
    next_agent: str  # Which agent to route to
    airtable_response: str  # Response from Airtable agent
    email_response: str  # Response from Email agent
    customer_info: dict  # Collected customer information
    needs_retry: bool  # Whether Airtable agent failed and needs retry


# ============================================================================
# Orchestrator Node - Routes requests using LLM
# ============================================================================

async def orchestrator_node(state: OrchestratorState) -> OrchestratorState:
    """
    Orchestrator decides which agent(s) to route to based on customer message
    Uses LLM to understand intent and make routing decisions
    """
    logger.info("🎯 Orchestrator analyzing customer message...")
    
    # Get the latest user message
    messages = state["messages"]
    latest_message = messages[-1].content if messages else ""
    
    # Create routing prompt
    routing_prompt = f"""Based on the customer's message, decide which agent(s) to use:

Customer message: "{latest_message}"

Available agents:
- "airtable" - Use for product queries, browsing, FAQs, store info
- "email" - Use for purchases, complaints, quotes, escalations (sends email to Nicole)
- "both" - Use when customer wants products AND wants to email/buy
- "respond" - Use when you can answer directly without agents (greetings, clarifications)

Respond with ONLY ONE WORD: airtable, email, both, or respond

Examples:
- "Show me sofas" → airtable
- "I want to buy this table" → email
- "Show me lamps and email them to Nicole" → both
- "Hello!" → respond
- "This product is defective!" → email

Decision:"""
    
    # Use Cohere to determine routing
    model = ChatCohere(
        model=COHERE_MODEL,
        cohere_api_key=COHERE_API_KEY,
        temperature=0.0
    )
    
    routing_response = await model.ainvoke([
        SystemMessage(content=ORCHESTRATOR_PROMPT),
        HumanMessage(content=routing_prompt)
    ])
    
    # Extract routing decision
    decision = routing_response.content.strip().lower()
    
    # Validate decision
    valid_decisions = ["airtable", "email", "both", "respond"]
    if decision not in valid_decisions:
        # Default to airtable if unclear
        logger.warning(f"⚠️ Invalid routing decision '{decision}', defaulting to 'airtable'")
        decision = "airtable"
    
    logger.info(f"📍 Routing decision: {decision}")
    
    state["next_agent"] = decision
    state["needs_retry"] = False
    
    return state


# ============================================================================
# Airtable Agent Node
# ============================================================================

async def airtable_node(state: OrchestratorState) -> OrchestratorState:
    """
    Call the Airtable agent to handle product queries and FAQs
    """
    logger.info("📊 Calling Airtable Agent...")
    
    try:
        airtable_agent = get_airtable_agent()
        
        # Prepare messages with system prompt
        agent_messages = [
            SystemMessage(content=AIRTABLE_AGENT_PROMPT)
        ] + state["messages"]
        
        # Invoke the Airtable agent
        response = await airtable_agent.ainvoke({"messages": agent_messages})
        
        # Extract the response
        if 'messages' in response and isinstance(response['messages'], list):
            for msg in reversed(response['messages']):
                if hasattr(msg, 'content') and msg.content:
                    if hasattr(msg, 'type') and msg.type == 'ai':
                        state["airtable_response"] = msg.content
                        logger.info(f"✅ Airtable Agent response: {msg.content[:100]}...")
                        break
        
        if not state.get("airtable_response"):
            state["airtable_response"] = "I couldn't retrieve product information at this time."
            state["needs_retry"] = True
            logger.warning("⚠️ Airtable Agent returned empty response")
        
    except Exception as e:
        logger.error(f"❌ Airtable Agent error: {e}")
        state["airtable_response"] = f"I'm having trouble accessing the product catalog."
        state["needs_retry"] = True
    
    return state


# ============================================================================
# Email Agent Node
# ============================================================================

async def email_node(state: OrchestratorState) -> OrchestratorState:
    """
    Call the Email agent to send emails to Nicole
    """
    logger.info("📧 Calling Email Agent...")
    
    try:
        email_agent = get_email_agent()
        
        # Prepare context for email agent with system prompt
        context_messages = [
            SystemMessage(content=EMAIL_AGENT_PROMPT)
        ] + state["messages"].copy()
        
        # Add Airtable response if available (for "both" routing)
        if state.get("airtable_response"):
            context_messages.append(
                AIMessage(content=f"[Products found by Airtable Agent]: {state['airtable_response']}")
            )
        
        # Invoke the Email agent
        logger.info("🔄 Invoking Email Agent with messages...")
        response = await email_agent.ainvoke({"messages": context_messages})
        
        # Debug: Log all messages in response
        logger.info(f"📩 Email Agent returned {len(response.get('messages', []))} messages")
        
        # Check if the GMAIL_SEND_EMAIL tool was actually called
        email_sent = False
        tool_call_found = False
        tool_message_content = ""
        
        if 'messages' in response and isinstance(response['messages'], list):
            # Check for tool calls in the response
            for i, msg in enumerate(response['messages']):
                msg_type = getattr(msg, 'type', 'unknown')
                logger.info(f"  Message {i}: type={msg_type}")
                
                # Check if this is a tool message (indicates tool was called)
                if msg_type == 'tool':
                    tool_call_found = True
                    tool_message_content = str(msg.content) if hasattr(msg, 'content') else ""
                    logger.info(f"  🔧 Tool message found: {tool_message_content[:200]}")
                    
                    # Check if it's a successful email send
                    content_lower = tool_message_content.lower()
                    if 'success' in content_lower or 'sent' in content_lower or 'message sent' in content_lower:
                        email_sent = True
                        logger.info("  ✅ Tool indicates success!")
                    else:
                        logger.warning(f"  ⚠️ Tool response doesn't indicate success: {tool_message_content[:100]}")
        
        # Extract the agent's response
        agent_response = ""
        if 'messages' in response and isinstance(response['messages'], list):
            for msg in reversed(response['messages']):
                if hasattr(msg, 'content') and msg.content:
                    if hasattr(msg, 'type') and msg.type == 'ai':
                        agent_response = msg.content
                        logger.info(f"🤖 Agent response: {agent_response[:150]}...")
                        break
        
        # Verify the agent actually sent the email
        if tool_call_found and email_sent:
            # Email was actually sent - use the agent's response
            state["email_response"] = agent_response or "✅ I've forwarded your request to Nicole. She'll contact you soon!"
            logger.info("✅✅✅ Email CONFIRMED sent successfully!")
        elif tool_call_found and not email_sent:
            # Tool was called but failed
            state["email_response"] = "I attempted to send the email but encountered an issue. Please contact Nicole directly at halfaouimedtej@gmail.com or call the store."
            logger.error(f"❌ Email tool was called but failed. Tool response: {tool_message_content[:200]}")
        else:
            # No tool was called - the agent didn't actually send anything
            state["email_response"] = "I apologize, but I wasn't able to send the email. Please contact Nicole directly at halfaouimedtej@gmail.com or call us at Blvd Morazán, Tegucigalpa."
            logger.error("❌❌❌ Email Agent did NOT call the GMAIL_SEND_EMAIL tool - it just hallucinated!")
        
    except Exception as e:
        logger.error(f"❌ Email Agent error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        state["email_response"] = "I apologize, but I'm having trouble sending the email right now. Please contact Nicole directly at halfaouimedtej@gmail.com"
    
    return state


# ============================================================================
# Response Node - Orchestrator provides direct response
# ============================================================================

async def respond_node(state: OrchestratorState) -> OrchestratorState:
    """
    Orchestrator responds directly for greetings, clarifications, etc.
    """
    logger.info("💬 Orchestrator responding directly...")
    
    try:
        model = ChatCohere(
            model=COHERE_MODEL,
            cohere_api_key=COHERE_API_KEY,
            temperature=0.3
        )
        
        # Get conversation history
        messages = [SystemMessage(content=ORCHESTRATOR_PROMPT)] + state["messages"]
        
        # Generate response
        response = await model.ainvoke(messages)
        
        # Add response to messages
        state["messages"].append(AIMessage(content=response.content))
        logger.info(f"✅ Direct response: {response.content[:100]}...")
        
    except Exception as e:
        logger.error(f"❌ Error in respond node: {e}")
        state["messages"].append(
            AIMessage(content="Hello! I'm Imi, your Proimi Home assistant. How can I help you today?")
        )
    
    return state


# ============================================================================
# Combiner Node - Combines agent responses
# ============================================================================

async def combiner_node(state: OrchestratorState) -> OrchestratorState:
    """
    Combines responses from specialized agents into a natural response
    """
    logger.info("🔗 Combining agent responses...")
    
    try:
        model = ChatCohere(
            model=COHERE_MODEL,
            cohere_api_key=COHERE_API_KEY,
            temperature=0.3
        )
        
        # Build context for combination
        combination_prompt = f"""You are Imi, Proimi Home's assistant. Combine the following responses into ONE natural, conversational message:

"""
        
        if state.get("airtable_response"):
            combination_prompt += f"""Airtable Agent (Products/FAQs):
{state['airtable_response']}

"""
        
        if state.get("email_response"):
            combination_prompt += f"""Email Agent (Email Status):
{state['email_response']}

"""
        
        combination_prompt += """Create a single, natural response that:
1. Presents product information if available
2. Confirms email status if applicable
3. Maintains Imi's warm, professional personality
4. Ends with clear next steps

Keep it conversational and WhatsApp-friendly. Don't mention "agents" or internal processes."""
        
        # Generate combined response
        response = await model.ainvoke([
            SystemMessage(content=ORCHESTRATOR_PROMPT),
            HumanMessage(content=combination_prompt)
        ])
        
        # Add combined response to messages
        state["messages"].append(AIMessage(content=response.content))
        logger.info(f"✅ Combined response: {response.content[:100]}...")
        
    except Exception as e:
        logger.error(f"❌ Error in combiner node: {e}")
        # Fallback: concatenate responses
        combined = ""
        if state.get("airtable_response"):
            combined += state["airtable_response"] + "\n\n"
        if state.get("email_response"):
            combined += state["email_response"]
        
        if combined:
            state["messages"].append(AIMessage(content=combined.strip()))
        else:
            state["messages"].append(
                AIMessage(content="I apologize, but I couldn't process your request. How else can I help you?")
            )
    
    return state


# ============================================================================
# Retry Handler Node
# ============================================================================

async def retry_handler_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle Airtable failures by offering to escalate to Nicole
    """
    logger.info("🔄 Handling Airtable failure...")
    
    try:
        model = ChatCohere(
            model=COHERE_MODEL,
            cohere_api_key=COHERE_API_KEY,
            temperature=0.3
        )
        
        retry_prompt = """The product catalog is temporarily unavailable. Respond to the customer with:
1. Apologize for the inconvenience
2. Offer to forward their inquiry to Nicole (sales manager)
3. Provide alternative: visit store, call, or try again later
4. Keep tone warm and helpful

Response:"""
        
        response = await model.ainvoke([
            SystemMessage(content=ORCHESTRATOR_PROMPT),
            HumanMessage(content=retry_prompt)
        ])
        
        state["messages"].append(AIMessage(content=response.content))
        logger.info(f"✅ Retry response: {response.content[:100]}...")
        
    except Exception as e:
        logger.error(f"❌ Error in retry handler: {e}")
        state["messages"].append(
            AIMessage(content="I apologize, but I'm having trouble accessing our catalog right now. Would you like me to forward your inquiry to Nicole, our sales manager? You can also visit us at Blvd Morazán or call during business hours.")
        )
    
    return state


# ============================================================================
# Router Functions
# ============================================================================

def route_after_orchestrator(state: OrchestratorState) -> Literal["airtable", "email", "both_airtable", "respond"]:
    """Route to appropriate agent based on orchestrator decision"""
    decision = state.get("next_agent", "respond")
    
    if decision == "both":
        return "both_airtable"  # First go to Airtable, then Email
    else:
        return decision


def route_after_airtable(state: OrchestratorState) -> Literal["email", "combiner", "retry"]:
    """Route after Airtable agent"""
    if state.get("needs_retry", False):
        return "retry"
    elif state.get("next_agent") == "both":
        return "email"  # Continue to email for "both" routing
    else:
        return "combiner"


def route_after_email(state: OrchestratorState) -> Literal["combiner"]:
    """Always go to combiner after email"""
    return "combiner"


# ============================================================================
# Build the Workflow Graph
# ============================================================================

def create_workflow():
    """Create the LangGraph workflow"""
    logger.info("🏗️  Building workflow graph...")
    
    # Create the graph
    workflow = StateGraph(OrchestratorState)
    
    # Add nodes
    workflow.add_node("orchestrator", orchestrator_node)
    workflow.add_node("airtable", airtable_node)
    workflow.add_node("email", email_node)
    workflow.add_node("respond", respond_node)
    workflow.add_node("combiner", combiner_node)
    workflow.add_node("retry", retry_handler_node)
    
    # Add edges
    workflow.add_edge(START, "orchestrator")
    
    # Conditional routing from orchestrator
    workflow.add_conditional_edges(
        "orchestrator",
        route_after_orchestrator,
        {
            "airtable": "airtable",
            "email": "email",
            "both_airtable": "airtable",
            "respond": "respond"
        }
    )
    
    # Routing from Airtable
    workflow.add_conditional_edges(
        "airtable",
        route_after_airtable,
        {
            "email": "email",
            "combiner": "combiner",
            "retry": "retry"
        }
    )
    
    # Routing from Email
    workflow.add_conditional_edges(
        "email",
        route_after_email,
        {
            "combiner": "combiner"
        }
    )
    
    # End edges
    workflow.add_edge("combiner", END)
    workflow.add_edge("respond", END)
    workflow.add_edge("retry", END)
    
    # Compile with memory
    memory = MemorySaver()
    compiled_workflow = workflow.compile(checkpointer=memory)
    
    logger.info("✅ Workflow graph built successfully!")
    
    return compiled_workflow


# ============================================================================
# Global workflow instance
# ============================================================================

_workflow = None


async def get_workflow():
    """Get or create the workflow instance"""
    global _workflow
    if _workflow is None:
        _workflow = create_workflow()
    return _workflow


# ============================================================================
# Main Process Function (called by WhatsApp server)
# ============================================================================

async def process_message(user_id: str, message: str) -> str:
    """
    Process a message from a user through the orchestrator workflow
    
    Args:
        user_id: Unique identifier for the user (WhatsApp ID)
        message: The user's message text
        
    Returns:
        The orchestrated response as a string
    """
    try:
        logger.info(f"📨 Processing message from user {user_id}: {message[:100]}...")
        
        # Get the workflow
        workflow = await get_workflow()
        
        # Create session config
        config = {
            "configurable": {
                "thread_id": f"whatsapp_{user_id}"
            }
        }
        
        # Prepare initial state
        initial_state = {
            "messages": [HumanMessage(content=message)],
            "next_agent": "",
            "airtable_response": "",
            "email_response": "",
            "customer_info": {},
            "needs_retry": False
        }
        
        # Run the workflow
        final_state = await workflow.ainvoke(initial_state, config=config)
        
        # Extract the final response
        response_text = ""
        if 'messages' in final_state and isinstance(final_state['messages'], list):
            # Get the last AI message
            for msg in reversed(final_state['messages']):
                if hasattr(msg, 'type') and msg.type == 'ai':
                    response_text = msg.content
                    break
        
        if not response_text:
            response_text = "I apologize, but I couldn't generate a response. Please try again."
        
        logger.info(f"✅ Final response for user {user_id}: {response_text[:100]}...")
        return response_text
        
    except Exception as e:
        logger.error(f"❌ Error processing message from user {user_id}: {e}")
        return "I apologize, but I encountered an error. Please try again in a moment, or contact us directly at Blvd Morazán, Tegucigalpa."