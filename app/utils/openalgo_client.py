"""
Extended OpenAlgo API client with additional methods
"""
import httpx
from openalgo import api


class ExtendedOpenAlgoAPI(api):
    """Extended OpenAlgo API client with ping method and optimized timeout"""

    def __init__(self, api_key, host="http://127.0.0.1:5000", version="v1", ws_port=8765, ws_url=None, timeout=30):
        """
        Initialize with a 30 second timeout (default).
        Uses positional args for super().__init__() to avoid FeedAPI MRO conflict.
        """
        super().__init__(api_key, host, version, ws_port, ws_url)
        self.timeout = timeout

    def _make_request(self, endpoint, payload):
        """Override to guarantee timeout is applied regardless of SDK version"""
        url = self.base_url + endpoint
        try:
            response = httpx.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            return self._handle_response(response)
        except httpx.TimeoutException:
            return {
                'status': 'error',
                'message': f'Request timed out after {self.timeout}s. The server took too long to respond.',
                'error_type': 'timeout_error'
            }
        except httpx.ConnectError:
            return {
                'status': 'error',
                'message': 'Failed to connect to the server. Please check if the server is running.',
                'error_type': 'connection_error'
            }
        except httpx.HTTPError as e:
            return {
                'status': 'error',
                'message': f'HTTP error occurred: {str(e)}',
                'error_type': 'http_error'
            }
        except Exception as e:
            return {
                'status': 'error',
                'message': f'An unexpected error occurred: {str(e)}',
                'error_type': 'unknown_error'
            }

    def ping(self):
        """
        Test connectivity and validate API key authentication
        
        This endpoint checks connectivity and validates the API key 
        authentication with the OpenAlgo platform.
        
        Returns:
            dict: Response with status, broker info, and message
            
        Example Response:
            {
                "data": {
                    "broker": "upstox",
                    "message": "pong"
                },
                "status": "success"
            }
        """
        payload = {"apikey": self.api_key}
        return self._make_request("ping", payload)