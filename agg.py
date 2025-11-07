"""
Agent Declarations for Proimi Home Multi-Agent System
- Airtable Agent: Product queries and FAQs
- Email Agent: Email sending to Nicole
FIXED: Simplified email agent to match working mail_airtable_mail.py architecture
"""

import logging
import os
from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_cohere import ChatCohere
from langgraph.prebuilt import create_react_agent
from prompg import AIRTABLE_AGENT_PROMPT, EMAIL_AGENT_PROMPT

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Get credentials from environment
AIRTABLE_API_KEY = "pat2uwDmO5pK0kkL6.46c90334f9c0d02e688565c548e97b19876d6fbb55f294f60e45287b27711381"
COHERE_API_KEY = "TQqDRXaJtVwT8HvcIc7cjaT5NaoUob6pXxT9jya6"
COMPOSIO_MCP_URL = "https://backend.composio.dev/v3/mcp/98ae1bdd-9bca-4582-80d6-f48fa1643dc5/mcp?include_composio_helper_actions=true"

# MCP Configuration
MCP_CONFIG = {
    "airtable": {
        "command": "npx",
        "args": ["-y", "airtable-mcp-server"],
        "env": {
            "AIRTABLE_API_KEY": AIRTABLE_API_KEY
        },
        "transport": "stdio"
    },
    "gmail": {
        "command": "npx",
        "args": [
            "-y",
            "mcp-remote",
            COMPOSIO_MCP_URL
        ],
        "env": {
            "npm_config_yes": "true"
        },
        "transport": "stdio"
    }
}

# Cohere Configuration
COHERE_MODEL = "command-a-03-2025"


class AgentFactory:
    """Factory for creating specialized agents"""
    
    def __init__(self):
        self.mcp_client = None
        self.airtable_tools = []
        self.gmail_tools = []
        self.all_tools = []  # Store all tools for email agent
        self.airtable_agent = None
        self.email_agent = None
        
    async def initialize(self):
        """Initialize MCP client and load tools"""
        try:
            logger.info("🔧 Initializing MCP client for Airtable and Gmail...")
            
            # Initialize the MultiServerMCPClient
            self.mcp_client = MultiServerMCPClient(MCP_CONFIG)
            
            # Load tools from both MCP servers
            logger.info("📦 Loading tools from MCP servers...")
            self.all_tools = await self.mcp_client.get_tools()
            logger.info(f"✅ Loaded {len(self.all_tools)} tools from MCP servers")
            
            # Separate tools by category
            self.airtable_tools = [
                t for t in self.all_tools 
                if any(keyword in t.name.lower() for keyword in ['airtable', 'base', 'table', 'record'])
            ]
            
            self.gmail_tools = [
                t for t in self.all_tools 
                if any(keyword in t.name.lower() for keyword in ['gmail', 'email', 'mail', 'send'])
            ]
            
            logger.info(f"📊 Airtable tools: {len(self.airtable_tools)}")
            for tool in self.airtable_tools:
                logger.info(f"  - {tool.name}")
            
            logger.info(f"📧 Gmail tools: {len(self.gmail_tools)}")
            for tool in self.gmail_tools:
                logger.info(f"  - {tool.name}")
            
            if len(self.gmail_tools) == 0:
                logger.error("❌ NO GMAIL TOOLS LOADED! Email functionality will not work!")
                logger.error("Check MCP configuration and Composio connection")
            
            # Create agents
            await self._create_airtable_agent()
            await self._create_email_agent()
            
            logger.info("✅ All agents initialized successfully!")
            
        except Exception as e:
            logger.error(f"❌ Error initializing agents: {e}")
            raise
    
    async def _create_airtable_agent(self):
        """Create the Airtable agent with read-only tools"""
        try:
            logger.info("🗺️ Creating Airtable Agent...")
            
            # Filter to only read-only tools (exclude create/update/delete)
            read_only_tools = [
                t for t in self.airtable_tools
                if not any(keyword in t.name.lower() for keyword in ['create', 'update', 'delete', 'modify'])
            ]
            
            logger.info(f"📖 Airtable Agent will use {len(read_only_tools)} read-only tools:")
            for tool in read_only_tools:
                logger.info(f"  ✓ {tool.name}")
            
            # Create Cohere model for Airtable agent
            model = ChatCohere(
                model=COHERE_MODEL,
                cohere_api_key=COHERE_API_KEY,
                temperature=0.1
            )
            
            # Create the agent
            self.airtable_agent = create_react_agent(model, read_only_tools)
            
            logger.info("✅ Airtable Agent created successfully!")
            
        except Exception as e:
            logger.error(f"❌ Error creating Airtable Agent: {e}")
            raise
    
    async def _create_email_agent(self):
        """Create the Email agent with ALL tools (like mail_airtable_mail.py)"""
        try:
            logger.info("🗺️ Creating Email Agent...")
            
            # CRITICAL FIX: Use ALL tools just like mail_airtable_mail.py does
            # This ensures GMAIL_SEND_EMAIL is definitely available
            logger.info(f"📧 Email Agent will use ALL {len(self.all_tools)} tools (including Gmail)")
            
            # Log Gmail tools specifically
            gmail_send_tools = [t for t in self.all_tools if 'send' in t.name.lower() and 'mail' in t.name.lower()]
            logger.info(f"📨 Found {len(gmail_send_tools)} Gmail send tools:")
            for tool in gmail_send_tools:
                logger.info(f"  ✓ {tool.name}")
            
            if len(gmail_send_tools) == 0:
                logger.error("❌ NO GMAIL SEND TOOLS FOUND! Email Agent will not be able to send emails!")
            
            # Create Cohere model for Email agent
            model = ChatCohere(
                model=COHERE_MODEL,
                cohere_api_key=COHERE_API_KEY,
                temperature=0.1
            )
            
            # Create the agent with ALL tools (just like mail_airtable_mail.py)
            self.email_agent = create_react_agent(model, self.all_tools)
            
            logger.info("✅ Email Agent created successfully with ALL tools!")
            
        except Exception as e:
            logger.error(f"❌ Error creating Email Agent: {e}")
            raise
    
    def get_airtable_agent(self):
        """Get the Airtable agent"""
        if self.airtable_agent is None:
            raise RuntimeError("Airtable agent not initialized. Call initialize() first.")
        return self.airtable_agent
    
    def get_email_agent(self):
        """Get the Email agent"""
        if self.email_agent is None:
            raise RuntimeError("Email agent not initialized. Call initialize() first.")
        return self.email_agent


# Global agent factory instance
agent_factory = AgentFactory()


async def initialize_agents():
    """Initialize all agents (call this on startup)"""
    await agent_factory.initialize()


def get_airtable_agent():
    """Get the initialized Airtable agent"""
    return agent_factory.get_airtable_agent()


def get_email_agent():
    """Get the initialized Email agent"""
    return agent_factory.get_email_agent()