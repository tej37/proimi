"""
WhatsApp Server for Proimi Home Multi-Agent System
Integrates with the orchestrator workflow
"""

import os
import asyncio
import fastapi
from pywa_async import WhatsApp, filters, types
import logging

# Import from the new architecture
from workflow import process_message
from agents import initialize_agents

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
fastapi_app = fastapi.FastAPI()

# Initialize WhatsApp client
wa = WhatsApp(
    phone_id="776079075579993",
    token="EAAb6PSYASM8BP3mBoZAbz3BypFhuzecKCyZCsY8P0YpZAoj9tUnXmafKjOhQnvE4tiuZAd6OLw6RcQBCzpj0CIGl2Nq3KXNYgC1vXKjbnlpW6ca7T9rjnWbUhLGEXPrO7drYjGjx4ZCGWPVGZA4bdtU3Lxv1JlrDFTU9XLnLFVByWrlXtsfN2VREwRc0o4eR0WagZDZD",
    server=fastapi_app,
    verify_token="testngrok",
)


@wa.on_message
async def handle_message(client: WhatsApp, msg: types.Message):
    """
    Handle incoming WhatsApp messages and respond with orchestrated AI content
    """
    try:
        logger.info(f"📱 Received message from {msg.from_user.wa_id}: {msg.text}")
        
        # Process message through the orchestrator workflow
        response = await process_message(msg.from_user.wa_id, msg.text)
        
        # Send the response back to the user
        await msg.reply_text(response)
        logger.info(f"✅ Sent response to {msg.from_user.wa_id}: {response[:100]}...")
        
    except Exception as e:
        logger.error(f"❌ Error handling message: {e}")
        try:
            await msg.reply_text(
                "I apologize, but I encountered an error. Please try again or contact us at:\n\n"
                "📍 Blvd Morazán, Tegucigalpa\n"
                "📧 halfaouimedtej@gmail.com\n"
                "⏰ Mon-Sat: 9:00 AM - 6:30 PM"
            )
        except:
            pass


@fastapi_app.on_event("startup")
async def startup_event():
    """Initialize all agents when the server starts"""
    logger.info("🚀 Starting Proimi Home WhatsApp Server...")
    logger.info("=" * 60)
    logger.info("🏗️  Initializing Multi-Agent System:")
    logger.info("   • Orchestrator (LangGraph)")
    logger.info("   • Airtable Agent (Product Queries)")
    logger.info("   • Email Agent (Gmail Integration)")
    logger.info("=" * 60)
    
    try:
        await initialize_agents()
        logger.info("✅ All agents initialized successfully!")
        logger.info("🎯 Imi is ready to assist customers!")
        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"❌ Failed to initialize agents: {e}")
        logger.warning("⚠️ Agents will be initialized on the first message.")


@fastapi_app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "Proimi Home WhatsApp AI Agent",
        "architecture": "Multi-Agent System (Orchestrator + Airtable + Email)",
        "version": "2.0"
    }


@fastapi_app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "agents": {
            "orchestrator": "active",
            "airtable": "active",
            "email": "active"
        }
    }


@fastapi_app.get("/status")
async def status():
    """Detailed status endpoint"""
    return {
        "service": "Proimi Home AI Assistant",
        "architecture": "Multi-Agent Orchestrator",
        "agents": {
            "orchestrator": {
                "role": "Routes requests and manages workflow",
                "status": "active"
            },
            "airtable_agent": {
                "role": "Product queries and FAQs (read-only)",
                "status": "active"
            },
            "email_agent": {
                "role": "Sends emails to Nicole (halfaouimedtej@gmail.com)",
                "status": "active"
            }
        },
        "integrations": {
            "whatsapp": "pywa_async",
            "mcp": ["airtable-mcp-server", "gmail-mcp-composio"],
            "llm": "Cohere Command-A-03-2025",
            "framework": "LangGraph + LangChain"
        }
    }


if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Starting server with uvicorn...")
    uvicorn.run(
        fastapi_app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )