"""
Flujo del Orquestador LangGraph para el sistema multi-agente Proimi Home
Dirige los mensajes de clientes a agentes especializados y gestiona el flujo de conversación
FIXED: Persiste correctamente el estado y continúa el flujo de correo después de recopilar información
"""

import logging
import os
import re
from typing import Annotated, TypedDict, Literal
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langchain_cohere import ChatCohere
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from agg import get_airtable_agent, get_email_agent, initialize_agents
from prompg import ORCHESTRATOR_PROMPT, AIRTABLE_AGENT_PROMPT, EMAIL_AGENT_PROMPT

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cohere configuration
COHERE_API_KEY = "TQqDRXaJtVwT8HvcIc7cjaT5NaoUob6pXxT9jya6"
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
    customer_info: dict  # Collected customer information (name, email, phone)
    needs_retry: bool  # Whether Airtable agent failed and needs retry
    needs_customer_info: bool  # Whether we need to collect customer info before email
    pending_email: bool  # Whether we have a pending email to send after collecting info


# ============================================================================ 
# Orchestrator Node - Routes requests using LLM
# ============================================================================

async def orchestrator_node(state: OrchestratorState) -> OrchestratorState:
    """
    Orquestador decide qué agente(s) utilizar según el mensaje del cliente
    Usa LLM para entender la intención y tomar decisiones de enrutamiento
    """
    logger.info("🎯 Orchestrator analyzing customer message...")
    
    # ⭐ CRITICAL FIX: Check if we're waiting for customer info (pending email)
    if state.get("pending_email", False):
        logger.info("⏳ PENDING EMAIL DETECTED - User should be providing info, routing to collect_info")
        state["next_agent"] = "collect_info"
        return state
    
    # Get the latest user message
    messages = state["messages"]
    latest_message = messages[-1].content if messages else ""
    
    # Create routing prompt (Español - Honduras)
    routing_prompt = f"""Según el mensaje del cliente, decide qué agente(s) usar:

Mensaje del cliente: "{latest_message}"

Contexto de la conversación reciente:
{chr(10).join([f"- {m.content[:100]}" for m in messages[-3:-1] if hasattr(m, 'content')])}

Agentes disponibles:
- "airtable" - Usar para consultas de producto ESPECÍFICAS con detalles O cuando el cliente ya está calificado (respondió preguntas)
- "email" - Usar para compras, reclamos, cotizaciones, escalaciones (envía correo a Nicole)
- "both" - Usar cuando el cliente quiere ver productos Y además desea enviar correo/comprar
- "respond" - Usar para:
  * Consultas AMPLIAS/GENERALES sobre productos que requieren calificación (ej., "quiero salas", "necesito recámara")
  * Preguntas frecuentes (horario, ubicación, envío, etc.)
  * Saludos y solicitudes de aclaración
  * Cuando hay que hacer preguntas de calificación antes de mostrar productos

🎯 LÓGICA CLAVE:
- Si la consulta de producto es AMPLIA/VAGA (sin detalles) → "respond" (hacer preguntas de calificación)
- Si la consulta es ESPECÍFICA (tiene detalles como estilo, color, tamaño) → "airtable"
- Si el cliente dice "muéstrame", "quiero ver" DESPUÉS de discutir necesidades → "airtable"
- Si el cliente insiste en ver productos de inmediato → "airtable"
- Si es un seguimiento después de preguntas de calificación → "airtable"

Ejemplos:
- "Quiero salas" → respond (amplio - hay que calificar)
- "¿Tenés comedores?" → respond (amplio - preguntar detalles)
- "Necesito recámara" → respond (amplio - calificar necesidades)
- "Sofá seccional gris moderno" → airtable (específico)
- "Mesa de comedor para 6 personas de madera" → airtable (específico)
- "Muéstrame todo lo que tenés" → airtable (insistente)
- "Show me sofas" (después de hablar de moderno, gris, 3 plazas) → airtable (calificado)
- "I want to buy this" → email
- "Store hours?" → respond (FAQ)
- "¡Hola!" → respond (saludo)

Responde con SOLO UNA PALABRA: airtable, email, both, o respond

Decisión:"""
    
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
    
    # Check if we need customer info for email routes
    if decision in ["email", "both"]:
        state["needs_customer_info"] = True
        state["pending_email"] = True  # ⭐ Mark that we'll need to send email
        logger.info("📧 Email route detected - marking pending_email=True")
    else:
        state["needs_customer_info"] = False
    
    return state


# ============================================================================ 
# Customer Info Collection Node
# ============================================================================

async def collect_customer_info_node(state: OrchestratorState) -> OrchestratorState:
    """
    Collect customer information (name, email, phone) before sending email
    """
    logger.info("📝 Collecting customer information...")
    logger.info(f"   Current pending_email: {state.get('pending_email', False)}")
    
    try:
        # Get current customer info
        customer_info = state.get("customer_info", {})
        
        # Get the latest message
        latest_message = ""
        for msg in reversed(state["messages"]):
            if hasattr(msg, 'type') and msg.type == 'human':
                latest_message = msg.content
                break
        
        logger.info(f"📄 Latest message: {latest_message[:100]}")
        
        # ⭐ ENHANCED EXTRACTION: Try multiple methods to extract info
        
        # Method 1: Regex extraction
        email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', latest_message)
        phone_match = re.search(r'\b[\d\-\+\(\)\s]{7,}\b', latest_message)
        
        if email_match and not customer_info.get("email"):
            customer_info["email"] = email_match.group(0)
            logger.info(f"✅ Extracted email: {customer_info['email']}")
        
        if phone_match and not customer_info.get("phone"):
            customer_info["phone"] = phone_match.group(0).strip()
            logger.info(f"✅ Extracted phone: {customer_info['phone']}")
        
        # Method 2: Extract name (text before email or comma-separated)
        if email_match and not customer_info.get("name"):
            name_text = latest_message[:email_match.start()].strip()
            # Remove common separators
            name_text = name_text.replace(',', '').strip()
            if name_text and len(name_text) > 1 and len(name_text) < 50:
                customer_info["name"] = name_text
                logger.info(f"✅ Extracted name: {customer_info['name']}")
        
        # Method 3: Parse comma-separated format (e.g., "tej, eizfe@gmail.com, 9823423")
        if ',' in latest_message and not all([customer_info.get("name"), customer_info.get("email"), customer_info.get("phone")]):
            parts = [p.strip() for p in latest_message.split(',')]
            logger.info(f"🔍 Trying comma-separated parsing: {parts}")
            
            for part in parts:
                # Email
                if '@' in part and not customer_info.get("email"):
                    customer_info["email"] = part
                    logger.info(f"✅ Extracted email from CSV: {part}")
                # Phone (numbers, dashes, spaces)
                elif re.search(r'[\d\-\+\(\)\s]{7,}', part) and not customer_info.get("phone"):
                    customer_info["phone"] = part
                    logger.info(f"✅ Extracted phone from CSV: {part}")
                # Name (first part without @ or numbers)
                elif not re.search(r'[@\d]', part) and len(part) > 1 and not customer_info.get("name"):
                    customer_info["name"] = part
                    logger.info(f"✅ Extracted name from CSV: {part}")
        
        # Method 4: Use LLM as fallback if still missing info
        if not customer_info.get("name") or not customer_info.get("email") or not customer_info.get("phone"):
            logger.info("🔍 Using LLM to extract customer info...")
            
            # Get recent conversation context (last 5 messages)
            recent_messages = []
            for msg in state["messages"][-5:]:
                if hasattr(msg, 'content'):
                    msg_type = "User" if hasattr(msg, 'type') and msg.type == 'human' else "Assistant"
                    recent_messages.append(f"{msg_type}: {msg.content}")
            
            conversation_text = "\n".join(recent_messages)
            
            model = ChatCohere(
                model=COHERE_MODEL,
                cohere_api_key=COHERE_API_KEY,
                temperature=0.0
            )
            
            extraction_prompt = f"""Extrae la información de contacto del cliente de esta conversación.

Mensajes recientes:
{conversation_text}

Busca:
- NOMBRE: Nombre completo del cliente
- EMAIL: Dirección de correo electrónico (contiene @)
- TELÉFONO: Número de teléfono (7+ dígitos)

Responde en ESTE FORMATO EXACTO (una por línea):
NAME: [nombre o MISSING]
EMAIL: [email o MISSING]
PHONE: [teléfono o MISSING]

Si no puedes encontrar algo, escribe MISSING."""
            
            extraction_response = await model.ainvoke([HumanMessage(content=extraction_prompt)])
            extraction_text = extraction_response.content
            
            logger.info(f"📊 LLM extraction result:\n{extraction_text}")
            
            # Parse LLM response
            for line in extraction_text.split("\n"):
                if "NAME:" in line and not customer_info.get("name"):
                    name = line.split("NAME:")[1].strip()
                    if name and name != "MISSING" and len(name) > 1:
                        customer_info["name"] = name
                        logger.info(f"✅ LLM extracted name: {name}")
                
                if "EMAIL:" in line and not customer_info.get("email"):
                    email = line.split("EMAIL:")[1].strip()
                    if email and email != "MISSING" and "@" in email:
                        customer_info["email"] = email
                        logger.info(f"✅ LLM extracted email: {email}")
                
                if "PHONE:" in line and not customer_info.get("phone"):
                    phone = line.split("PHONE:")[1].strip()
                    if phone and phone != "MISSING" and len(phone) >= 7:
                        customer_info["phone"] = phone
                        logger.info(f"✅ LLM extracted phone: {phone}")
        
        # ⭐ Update state with collected info
        state["customer_info"] = customer_info
        
        # Check what we have
        has_name = customer_info.get("name")
        has_email = customer_info.get("email")
        has_phone = customer_info.get("phone")
        
        logger.info(f"📋 Customer info status - Name: {bool(has_name)}, Email: {bool(has_email)}, Phone: {bool(has_phone)}")
        
        # Determine what's missing
        missing = []
        if not has_name:
            missing.append("nombre completo")
        if not has_email:
            missing.append("dirección de correo")
        if not has_phone:
            missing.append("número de teléfono")
        
        if missing:
            logger.info(f"⚠️ Still missing: {missing}")
            ask_message = f"Antes de que pueda reenviarlo a Nicole, ¿me puede dar por favor su {', '.join(missing)}?"
            state["messages"].append(AIMessage(content=ask_message))
            state["needs_customer_info"] = True
            # ⭐ Keep pending_email=True so we continue the workflow on next message
            logger.info("📌 Keeping pending_email=True, waiting for user to provide missing info")
        else:
            logger.info("✅✅✅ ALL CUSTOMER INFO COLLECTED!")
            logger.info(f"   Name: {customer_info.get('name')}")
            logger.info(f"   Email: {customer_info.get('email')}")
            logger.info(f"   Phone: {customer_info.get('phone')}")
            state["needs_customer_info"] = False
            # ⭐ Keep pending_email=True so we proceed to send email
            logger.info("📨 Will proceed to send email in next step")
        
    except Exception as e:
        logger.error(f"❌ Error collecting customer info: {e}")
        import traceback
        logger.error(traceback.format_exc())
        state["needs_customer_info"] = True
    
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
            state["airtable_response"] = "No pude obtener información de productos en este momento."
            state["needs_retry"] = True
            logger.warning("⚠️ Airtable Agent returned empty response")
        
    except Exception as e:
        logger.error(f"❌ Airtable Agent error: {e}")
        state["airtable_response"] = "Tengo problemas para acceder al catálogo de productos."
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
    logger.info("=" * 80)
    
    try:
        email_agent = get_email_agent()
        
        # Get customer info
        customer_info = state.get("customer_info", {})
        customer_name = customer_info.get("name", "Cliente")
        customer_email = customer_info.get("email", "No proporcionado")
        customer_phone = customer_info.get("phone", "No proporcionado")
        
        logger.info(f"👤 Customer: {customer_name}")
        logger.info(f"📧 Email: {customer_email}")
        logger.info(f"📱 Phone: {customer_phone}")
        
        # Extract customer request from conversation
        customer_request = ""
        for msg in state["messages"]:
            if hasattr(msg, 'type') and msg.type == 'human':
                # Look for purchase/inquiry intent messages
                content = msg.content.lower()
                if any(word in content for word in ['buy', 'comprar', 'purchase', 'want', 'quiero', 'interested', 'price', 'how much']):
                    customer_request = msg.content
                    break
        
        if not customer_request:
            # Get the first substantive user message
            for msg in state["messages"]:
                if hasattr(msg, 'type') and msg.type == 'human':
                    # Skip info-only messages
                    if not re.search(r'[@\d]', msg.content):
                        customer_request = msg.content
                        break
            
            # If still empty, use latest message
            if not customer_request:
                for msg in reversed(state["messages"]):
                    if hasattr(msg, 'type') and msg.type == 'human':
                        customer_request = msg.content
                        break
        
        logger.info(f"📝 Customer request: {customer_request[:100]}...")
        
        # Prepare email content (Español)
        email_subject = f"Proimi – Consulta de cliente: {customer_name}"
        
        email_body = f"""Hola Nicole,

Nueva consulta de cliente desde WhatsApp:

DATOS DEL CLIENTE:
- Nombre: {customer_name}
- Correo: {customer_email}
- Teléfono: {customer_phone}
- Canal: WhatsApp

SOLICITUD DEL CLIENTE:
{customer_request}

"""
        
        # Add product context if available
        if state.get("airtable_response"):
            products_text = state['airtable_response'][:800]
            email_body += f"""PRODUCTOS MENCIONADOS:
{products_text}...

"""
        
        email_body += """ACCIÓN RECOMENDADA:
Por favor darle seguimiento al cliente lo antes posible.

Saludos,
Imi - Asistente virtual de Proimi Home"""
        
        logger.info(f"📧 Preparing email to: halfaouimedtej@gmail.com")
        logger.info(f"📋 Subject: {email_subject}")
        
        # Create instruction for agent (keep tool name clear)
        email_instruction = f"""You MUST send an email using the GMAIL_SEND_EMAIL tool RIGHT NOW.

Recipient: halfaouimedtej@gmail.com
Subject: {email_subject}
Body:
{email_body}

CRITICAL INSTRUCTION: Call the GMAIL_SEND_EMAIL tool immediately with these parameters:
- to: "halfaouimedtej@gmail.com"
- subject: "{email_subject}"
- body: "{email_body}"

DO NOT respond with text - CALL THE TOOL FIRST, then confirm success."""
        
        # Prepare messages
        agent_messages = [
            SystemMessage(content=EMAIL_AGENT_PROMPT),
            HumanMessage(content=email_instruction)
        ]
        
        logger.info("📤 Invoking Email Agent with GMAIL_SEND_EMAIL instruction...")
        
        # Invoke the agent
        response = await email_agent.ainvoke(
            {"messages": agent_messages},
            config={
                "recursion_limit": 20,
                "max_iterations": 15
            }
        )
        
        # Check tool execution
        logger.info(f"📩 Email Agent returned {len(response.get('messages', []))} messages")
        logger.info("-" * 80)
        
        email_sent = False
        tool_called = False
        
        if 'messages' in response and isinstance(response['messages'], list):
            for i, msg in enumerate(response['messages']):
                msg_type = getattr(msg, 'type', 'unknown')
                logger.info(f"  Message {i}: type={msg_type}")
                
                # Check for tool calls
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    tool_called = True
                    logger.info(f"  🔧 Tool calls found: {len(msg.tool_calls)}")
                    for tc in msg.tool_calls:
                        tool_name = getattr(tc, 'name', 'unknown')
                        logger.info(f"     - Tool: {tool_name}")
                
                # Check for tool response
                if msg_type == 'tool':
                    tool_content = str(msg.content) if hasattr(msg, 'content') else ""
                    logger.info(f"  📧 Tool response: {tool_content[:300]}")
                    
                    # Check for success indicators
                    success_words = ['success', 'sent', 'id', 'threadid', 'message sent']
                    if any(word in tool_content.lower() for word in success_words):
                        email_sent = True
                        logger.info("  ✅ SUCCESS INDICATORS FOUND IN TOOL RESPONSE!")
        
        logger.info("-" * 80)
        logger.info(f"📊 FINAL STATUS - Tool called: {tool_called}, Email sent: {email_sent}")
        logger.info("=" * 80)
        
        # ⭐ Clear pending email flag after sending (or attempting to send)
        state["pending_email"] = False
        logger.info("📌 Cleared pending_email flag")
        
        # Determine response (Español)
        if tool_called and email_sent:
            state["email_response"] = (
                f"✅ ¡Perfecto! He reenviado tu solicitud a Nicole con tus datos:\n"
                f"• Nombre: {customer_name}\n"
                f"• Correo: {customer_email}\n"
                f"• Teléfono: {customer_phone}\n\n"
                f"Nicole se pondrá en contacto pronto. Mientras tanto, puedes contactarla en:\n"
                f"📧 halfaouimedtej@gmail.com\n"
                f"📍 Blvd Morazán, Tegucigalpa\n"
                f"🕒 Lun-Sáb: 9:00 AM - 6:30 PM"
            )
            logger.info("✅✅✅ EMAIL SENT SUCCESSFULLY!")
        else:
            state["email_response"] = (
                f"Lo siento, estoy teniendo problemas para enviar el correo automáticamente. "
                f"Por favor contacta a Nicole directamente:\n\n"
                f"📧 halfaouimedtej@gmail.com\n"
                f"📍 Blvd Morazán, Tegucigalpa\n"
                f"🕒 Lun-Sáb: 9:00 AM - 6:30 PM\n\n"
                f"Tus datos para compartir con ella:\n"
                f"• Nombre: {customer_name}\n"
                f"• Correo: {customer_email}\n"
                f"• Teléfono: {customer_phone}"
            )
            logger.error(f"❌ EMAIL FAILED - Tool called: {tool_called}, Sent: {email_sent}")
        
    except Exception as e:
        logger.error(f"❌ Email Agent error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        
        state["pending_email"] = False
        state["email_response"] = (
            "Lo siento, tengo problemas para enviar correos ahora. "
            "Por favor contacta a Nicole directamente:\n\n"
            "📧 halfaouimedtej@gmail.com\n"
            "📍 Blvd Morazán, Tegucigalpa\n"
            "🕒 Lun-Sáb: 9:00 AM - 6:30 PM"
        )
    
    return state


# ============================================================================ 
# Response Node
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
            AIMessage(content="¡Hola! Soy Imi, tu asistente de Proimi Home. ¿En qué puedo ayudarte?")
        )
    
    return state


# ============================================================================ 
# Combiner Node
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
        
        # Build context for combination (Español)
        combination_prompt = f"""Eres Imi, la asistente de Proimi Home. Combina las siguientes respuestas en UN único mensaje natural y conversacional:

"""
        
        if state.get("airtable_response"):
            combination_prompt += f"""Airtable (Productos/FAQs):
{state['airtable_response']}

"""
        
        if state.get("email_response"):
            combination_prompt += f"""Email (Estado del envío):
{state['email_response']}

"""
        
        combination_prompt += """Crea una única respuesta natural que:
1. Presente información de productos si está disponible
2. Confirme el estado del correo si aplica
3. Mantenga la personalidad cálida y profesional de Imi
4. Termine con pasos claros a seguir

Mantenlo conversacional y apto para WhatsApp. No menciones 'agentes' ni procesos internos."""
        
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
                AIMessage(content="Lo siento, no pude procesar tu solicitud. ¿En qué más puedo ayudarte?")
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
        
        retry_prompt = """El catálogo de productos no está disponible temporalmente. Responde al cliente con:
1. Pedir disculpas por el inconveniente
2. Ofrecer reenviar su consulta a Nicole (encargada de ventas)
3. Ofrecer alternativas: visitar la tienda, llamar, o intentar más tarde
4. Mantener tono cálido y servicial

Respuesta:"""
        
        response = await model.ainvoke([
            SystemMessage(content=ORCHESTRATOR_PROMPT),
            HumanMessage(content=retry_prompt)
        ])
        
        state["messages"].append(AIMessage(content=response.content))
        logger.info(f"✅ Retry response: {response.content[:100]}...")
        
    except Exception as e:
        logger.error(f"❌ Error in retry handler: {e}")
        state["messages"].append(
            AIMessage(content="Disculpa, ahora mismo no puedo acceder al catálogo. ¿Quieres que le reenvíe tu consulta a Nicole?")
        )
    
    return state


# ============================================================================ 
# Router Functions
# ============================================================================

def route_after_orchestrator(state: OrchestratorState) -> Literal["airtable", "collect_info", "both_airtable", "respond"]:
    """Route to appropriate agent based on orchestrator decision"""
    decision = state.get("next_agent", "respond")
    
    logger.info(f"🔀 Routing after orchestrator: decision={decision}, pending_email={state.get('pending_email', False)}")
    
    # ⭐ CRITICAL: Special handling for when we routed to collect_info due to pending_email
    if decision == "collect_info":
        logger.info("   → Routing to collect_info (from pending_email check)")
        return "collect_info"
    
    # If email route and need customer info, collect it first
    if decision in ["email", "both"] and state.get("needs_customer_info", True):
        logger.info("   → Routing to collect_info (need customer info for email)")
        return "collect_info"
    
    if decision == "both":
        logger.info("   → Routing to both_airtable (will do airtable then email)")
        return "both_airtable"
    else:
        logger.info(f"   → Routing to {decision}")
        return decision


def route_after_info_collection(state: OrchestratorState) -> Literal["email", "END"]:
    """Route after collecting customer info"""
    needs_info = state.get("needs_customer_info", False)
    
    logger.info(f"🔀 Routing after info collection: needs_info={needs_info}, pending_email={state.get('pending_email', False)}")
    
    if needs_info:
        # Still need info, end and wait for user response
        logger.info("   → END (still need customer info, waiting for user)")
        return "END"
    else:
        # Have all info, proceed to email
        logger.info("   → email (have all info, proceeding to send)")
        return "email"


def route_after_airtable(state: OrchestratorState) -> Literal["collect_info", "email", "combiner", "retry"]:
    """Route after Airtable agent"""
    logger.info(f"🔀 Routing after airtable: needs_retry={state.get('needs_retry', False)}, next_agent={state.get('next_agent')}")
    
    if state.get("needs_retry", False):
        logger.info("   → retry (airtable failed)")
        return "retry"
    elif state.get("next_agent") == "both":
        # Check if we need customer info for email
        if state.get("needs_customer_info", True):
            logger.info("   → collect_info (both route, need customer info)")
            return "collect_info"
        else:
            logger.info("   → email (both route, have customer info)")
            return "email"
    else:
        logger.info("   → combiner (airtable only)")
        return "combiner"


def route_after_email(state: OrchestratorState) -> Literal["combiner"]:
    """Always go to combiner after email"""
    logger.info("🔀 Routing after email → combiner")
    return "combiner"


# ============================================================================ 
# Build the Workflow Graph
# ============================================================================

def create_workflow():
    """Create the LangGraph workflow"""
    logger.info("🗺️ Building workflow graph...")
    
    # Create the graph
    workflow = StateGraph(OrchestratorState)
    
    # Add nodes
    workflow.add_node("orchestrator", orchestrator_node)
    workflow.add_node("collect_info", collect_customer_info_node)
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
            "collect_info": "collect_info",
            "both_airtable": "airtable",
            "respond": "respond"
        }
    )
    
    # Routing from info collection
    workflow.add_conditional_edges(
        "collect_info",
        route_after_info_collection,
        {
            "email": "email",
            "END": END
        }
    )
    
    # Routing from Airtable
    workflow.add_conditional_edges(
        "airtable",
        route_after_airtable,
        {
            "collect_info": "collect_info",
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
# Main Process Function
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
        
        # Create session config (enables state persistence)
        config = {
            "configurable": {
                "thread_id": f"whatsapp_{user_id}"
            }
        }
        
        # ⭐ CRITICAL: Get existing state from memory first (if it exists)
        # This ensures we continue from where we left off
        try:
            # Try to get the current state
            existing_state = await workflow.aget_state(config)
            
            if existing_state and existing_state.values:
                logger.info("📚 Found existing conversation state - continuing workflow")
                logger.info(f"   Pending email: {existing_state.values.get('pending_email', False)}")
                logger.info(f"   Customer info: {existing_state.values.get('customer_info', {})}")
                
                # Continue with new message added to existing state
                initial_state = {
                    "messages": [HumanMessage(content=message)],
                }
            else:
                logger.info("🆕 No existing state - starting new conversation")
                # New conversation
                initial_state = {
                    "messages": [HumanMessage(content=message)],
                    "next_agent": "",
                    "airtable_response": "",
                    "email_response": "",
                    "customer_info": {},
                    "needs_retry": False,
                    "needs_customer_info": False,
                    "pending_email": False
                }
        except Exception as e:
            logger.warning(f"⚠️ Couldn't get existing state: {e}")
            # Start fresh if we can't get state
            initial_state = {
                "messages": [HumanMessage(content=message)],
                "next_agent": "",
                "airtable_response": "",
                "email_response": "",
                "customer_info": {},
                "needs_retry": False,
                "needs_customer_info": False,
                "pending_email": False
            }
        
        # Run the workflow
        logger.info("🔄 Invoking workflow...")
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
            response_text = "Lo siento, no pude generar una respuesta. Por favor intenta de nuevo."
        
        logger.info(f"✅ Final response for user {user_id}: {response_text[:100]}...")
        logger.info(f"📊 Final state - pending_email: {final_state.get('pending_email', False)}")
        
        return response_text
        
    except Exception as e:
        logger.error(f"❌ Error processing message from user {user_id}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return "Lo siento, encontré un error. Intenta de nuevo en un momento o contáctanos directamente en Blvd Morazán, Tegucigalpa."