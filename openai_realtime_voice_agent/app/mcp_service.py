"""MCP service integration using Pipecat's MCPClient with StreamableHTTP."""
import logging
from typing import Optional
from pipecat.services.mcp_service import MCPClient, StreamableHttpParameters

logger = logging.getLogger(__name__)


class HomeAssistantMCPClient(MCPClient):
    """Keep the SDK result envelope for the existing shared result classifier."""

    async def _call_tool(self, session, function_name, arguments, result_callback):
        # Pipecat 0.0.97 flattens content to text and drops isError. HA 2026.10
        # uses that flag independently of its JSON data. Reuse SDK transport,
        # discovery and registration; only replace the lossy result callback.
        # Let transport exceptions reach Pipecat's existing wrapper, without
        # retrying a write whose outcome may already have been committed.
        result = await session.call_tool(function_name, arguments=arguments)
        await result_callback(result.model_dump(mode="json", exclude_none=True))


class HomeAssistantMCPService:
    """Home Assistant MCP service using Pipecat's MCPClient."""
    
    def __init__(self, url: str, access_token: str):
        """
        Initialize Home Assistant MCP service.
        
        Args:
            url: Home Assistant MCP Server URL (e.g., http://supervisor/core/api/mcp)
            access_token: Long-lived access token for Home Assistant
        """
        self.url = url
        self.access_token = access_token
        self.mcp_client: Optional[MCPClient] = None
        
    async def initialize(self) -> MCPClient:
        """Initialize and return the MCP client."""
        try:
            logger.info(f"🔗 Initializing Home Assistant MCP Client at {self.url}")
            
            # Create StreamableHTTP parameters with authentication
            server_params = StreamableHttpParameters(
                url=self.url,
                headers={
                    "Authorization": f"Bearer {self.access_token}"
                }
            )
            
            # Create MCP client
            self.mcp_client = HomeAssistantMCPClient(server_params=server_params)
            
            logger.info("✅ Home Assistant MCP Client initialized")
            return self.mcp_client
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Home Assistant MCP Client: {e}", exc_info=True)
            raise
    
    def get_client(self) -> Optional[MCPClient]:
        """Get the MCP client instance."""
        return self.mcp_client





