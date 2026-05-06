"""deepagents_mongodb_fs: MongoDB Atlas-backed filesystem adapter for LangChain DeepAgents."""
from deepagents_mongodb_fs.backend import MongoFilesystemBackend
from deepagents_mongodb_fs.errors import AdapterError, ErrorCode

__version__ = "0.1.0"
__all__ = ["MongoFilesystemBackend", "AdapterError", "ErrorCode"]
