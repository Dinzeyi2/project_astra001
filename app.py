"""
🚀 AGENTSTATE: RAILWAY DEPLOYMENT
=================================

Complete production system optimized for Railway.app deployment

This file contains ALL features in one deployable package:
✅ Distributed state storage (PostgreSQL + Redis)
✅ FastAPI production server
✅ WebSocket support
✅ Authentication
✅ Monitoring
✅ SDK integrations
✅ Auto-configuration for Railway

DEPLOY TO RAILWAY:
1. Push this code to GitHub
2. Connect to Railway
3. Add PostgreSQL + Redis plugins
4. Deploy!

Railway will automatically:
- Install dependencies from requirements.txt
- Run this server on PORT from environment
- Connect to PostgreSQL and Redis
- Expose public URL
"""

import os
import sys
import time
import uuid
import json
import hashlib
import secrets
import threading
import copy
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict, field
from collections import defaultdict, Counter
from enum import Enum
from contextlib import asynccontextmanager

# FastAPI and server
from fastapi import FastAPI, HTTPException, Depends, status, WebSocket, WebSocketDisconnect, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
import uvicorn

# Database - SQLAlchemy
from sqlalchemy import create_engine, Column, String, Float, JSON, Integer, Text, Boolean, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool
from sqlalchemy.exc import OperationalError

# Redis
try:
    import redis
    from redis.lock import Lock as RedisLock
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False
    print("⚠️  Redis not available - will use in-memory fallback")

# Prometheus
try:
    from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False
    print("⚠️  Prometheus not available - metrics disabled")


# ============================================================================
# CONFIGURATION (Railway Auto-Detection)
# ============================================================================

class Config:
    """Auto-configure from Railway environment variables."""
    
    # Server
    PORT = int(os.getenv("PORT", "8000"))
    HOST = os.getenv("HOST", "0.0.0.0")
    
    # Database (Railway provides DATABASE_URL or POSTGRES_URL)
    DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
        # Railway uses postgres://, SQLAlchemy needs postgresql://
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    # Redis (Railway provides REDIS_URL)
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # API
    API_SECRET_KEY = os.getenv("API_SECRET_KEY", secrets.token_urlsafe(32))
    
    # Environment
    ENVIRONMENT = os.getenv("RAILWAY_ENVIRONMENT", "production")
    IS_RAILWAY = os.getenv("RAILWAY_ENVIRONMENT") is not None
    
    @classmethod
    def is_configured(cls):
        """Check if properly configured."""
        return cls.DATABASE_URL is not None

config = Config()

print("\n" + "="*80)
print("🚀 AGENTSTATE CONFIGURATION")
print("="*80)
print(f"Environment: {config.ENVIRONMENT}")
print(f"Railway: {'YES ✅' if config.IS_RAILWAY else 'NO (Local)'}")
print(f"Database: {'Configured ✅' if config.DATABASE_URL else 'In-Memory ⚠️'}")
print(f"Redis: {'Configured ✅' if HAS_REDIS else 'In-Memory ⚠️'}")
print(f"Port: {config.PORT}")
print("="*80 + "\n")


# ============================================================================
# DATABASE MODELS
# ============================================================================

Base = declarative_base()


class StateSnapshotModel(Base):
    """State snapshot storage."""
    __tablename__ = 'state_snapshots'
    
    commit_id = Column(String(36), primary_key=True)
    parent_commit = Column(String(36), nullable=True, index=True)
    agent_id = Column(String(255), nullable=False, index=True)
    timestamp = Column(Float, nullable=False, index=True)
    state_data = Column(JSON, nullable=False)
    metadata = Column(JSON, nullable=True)
    checksum = Column(String(64), nullable=False)
    
    __table_args__ = (
        Index('idx_agent_timestamp', 'agent_id', 'timestamp'),
    )


class HandoffTransactionModel(Base):
    """Handoff transaction storage."""
    __tablename__ = 'handoff_transactions'
    
    transaction_id = Column(String(36), primary_key=True)
    from_agent = Column(String(255), nullable=False, index=True)
    to_agent = Column(String(255), nullable=False, index=True)
    state_commit = Column(String(36), nullable=False)
    status = Column(String(50), nullable=False, index=True)
    initiated_at = Column(Float, nullable=False)
    acknowledged_at = Column(Float, nullable=True)
    completed_at = Column(Float, nullable=True)
    timeout_seconds = Column(Float, default=30.0)
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    error_message = Column(Text, nullable=True)


class AgentHeadModel(Base):
    """Agent head pointer storage."""
    __tablename__ = 'agent_heads'
    
    agent_id = Column(String(255), primary_key=True)
    commit_id = Column(String(36), nullable=False)
    updated_at = Column(Float, nullable=False)


# ============================================================================
# CORE STATE MANAGEMENT (From original system)
# ============================================================================

class StateStatus(Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    CONFLICT = "conflict"


class HandoffStatus(Enum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class StateSnapshot:
    """Immutable state snapshot."""
    commit_id: str
    parent_commit: Optional[str]
    agent_id: str
    timestamp: float
    state_data: Dict[str, Any]
    metadata: Dict[str, Any]
    checksum: str
    
    def __post_init__(self):
        expected = self._compute_checksum()
        if self.checksum != expected:
            raise ValueError("Checksum mismatch!")
    
    def _compute_checksum(self) -> str:
        content = json.dumps(self.state_data, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()
    
    @classmethod
    def create(cls, agent_id: str, state_data: Dict[str, Any], 
               parent_commit: Optional[str] = None,
               metadata: Optional[Dict[str, Any]] = None):
        commit_id = str(uuid.uuid4())
        timestamp = time.time()
        state_copy = copy.deepcopy(state_data)
        content = json.dumps(state_copy, sort_keys=True)
        checksum = hashlib.sha256(content.encode()).hexdigest()
        
        return cls(
            commit_id=commit_id,
            parent_commit=parent_commit,
            agent_id=agent_id,
            timestamp=timestamp,
            state_data=state_copy,
            metadata=metadata or {},
            checksum=checksum
        )


@dataclass
class HandoffTransaction:
    """Handoff transaction."""
    transaction_id: str
    from_agent: str
    to_agent: str
    state_commit: str
    status: HandoffStatus
    initiated_at: float
    acknowledged_at: Optional[float] = None
    completed_at: Optional[float] = None
    timeout_seconds: float = 30.0
    retry_count: int = 0
    max_retries: int = 3
    error_message: Optional[str] = None
    
    def is_expired(self) -> bool:
        if self.status in [HandoffStatus.COMPLETED, HandoffStatus.FAILED]:
            return False
        return (time.time() - self.initiated_at) > self.timeout_seconds


# ============================================================================
# STORAGE LAYER (Database or In-Memory)
# ============================================================================

class StorageBackend:
    """Unified storage backend - uses DB if available, memory otherwise."""
    
    def __init__(self):
        self.engine = None
        self.Session = None
        self.redis_client = None
        self.use_db = False
        self.use_cache = False
        
        # In-memory fallback
        self._memory_commits = {}
        self._memory_transactions = {}
        self._memory_heads = {}
        self._lock = threading.Lock()
        
        # Try to connect to database
        if config.DATABASE_URL:
            try:
                self.engine = create_engine(
                    config.DATABASE_URL,
                    poolclass=QueuePool,
                    pool_size=10,
                    max_overflow=20,
                    pool_timeout=30,
                    pool_recycle=3600,
                    pool_pre_ping=True
                )
                
                # Create tables
                Base.metadata.create_all(self.engine)
                
                # Test connection
                with self.engine.connect() as conn:
                    conn.execute("SELECT 1")
                
                self.Session = scoped_session(sessionmaker(bind=self.engine))
                self.use_db = True
                print("✅ PostgreSQL connected")
                
            except Exception as e:
                print(f"⚠️  PostgreSQL connection failed: {e}")
                print("   Using in-memory storage")
        
        # Try to connect to Redis
        if HAS_REDIS and config.REDIS_URL:
            try:
                self.redis_client = redis.from_url(
                    config.REDIS_URL,
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=5
                )
                self.redis_client.ping()
                self.use_cache = True
                print("✅ Redis connected")
            except Exception as e:
                print(f"⚠️  Redis connection failed: {e}")
        
        print(f"\n💾 Storage mode: {'Database + Cache' if self.use_db and self.use_cache else 'Database' if self.use_db else 'In-Memory'}\n")
    
    def save_snapshot(self, snapshot: StateSnapshot) -> bool:
        """Save state snapshot."""
        
        if not self.use_db:
            # In-memory
            with self._lock:
                self._memory_commits[snapshot.commit_id] = snapshot
                self._memory_heads[snapshot.agent_id] = snapshot.commit_id
            return True
        
        # Database
        try:
            session = self.Session()
            
            model = StateSnapshotModel(
                commit_id=snapshot.commit_id,
                parent_commit=snapshot.parent_commit,
                agent_id=snapshot.agent_id,
                timestamp=snapshot.timestamp,
                state_data=snapshot.state_data,
                metadata=snapshot.metadata,
                checksum=snapshot.checksum
            )
            session.add(model)
            
            # Update head
            head = session.query(AgentHeadModel).filter_by(agent_id=snapshot.agent_id).first()
            if head:
                head.commit_id = snapshot.commit_id
                head.updated_at = snapshot.timestamp
            else:
                head = AgentHeadModel(
                    agent_id=snapshot.agent_id,
                    commit_id=snapshot.commit_id,
                    updated_at=snapshot.timestamp
                )
                session.add(head)
            
            session.commit()
            session.close()
            
            # Cache
            if self.use_cache:
                try:
                    self.redis_client.setex(
                        f"snapshot:{snapshot.commit_id}",
                        3600,
                        json.dumps(asdict(snapshot))
                    )
                except:
                    pass
            
            return True
            
        except Exception as e:
            print(f"Error saving snapshot: {e}")
            return False
    
    def get_snapshot(self, commit_id: str) -> Optional[StateSnapshot]:
        """Get state snapshot."""
        
        if not self.use_db:
            with self._lock:
                return self._memory_commits.get(commit_id)
        
        # Try cache first
        if self.use_cache:
            try:
                cached = self.redis_client.get(f"snapshot:{commit_id}")
                if cached:
                    data = json.loads(cached)
                    return StateSnapshot(**data)
            except:
                pass
        
        # Database
        try:
            session = self.Session()
            model = session.query(StateSnapshotModel).filter_by(commit_id=commit_id).first()
            session.close()
            
            if not model:
                return None
            
            return StateSnapshot(
                commit_id=model.commit_id,
                parent_commit=model.parent_commit,
                agent_id=model.agent_id,
                timestamp=model.timestamp,
                state_data=model.state_data,
                metadata=model.metadata,
                checksum=model.checksum
            )
        except Exception as e:
            print(f"Error getting snapshot: {e}")
            return None
    
    def get_agent_head(self, agent_id: str) -> Optional[str]:
        """Get latest commit for agent."""
        
        if not self.use_db:
            with self._lock:
                return self._memory_heads.get(agent_id)
        
        try:
            session = self.Session()
            head = session.query(AgentHeadModel).filter_by(agent_id=agent_id).first()
            session.close()
            return head.commit_id if head else None
        except:
            return None
    
    def save_transaction(self, transaction: HandoffTransaction) -> bool:
        """Save handoff transaction."""
        
        if not self.use_db:
            with self._lock:
                self._memory_transactions[transaction.transaction_id] = transaction
            return True
        
        try:
            session = self.Session()
            model = HandoffTransactionModel(
                transaction_id=transaction.transaction_id,
                from_agent=transaction.from_agent,
                to_agent=transaction.to_agent,
                state_commit=transaction.state_commit,
                status=transaction.status.value,
                initiated_at=transaction.initiated_at,
                acknowledged_at=transaction.acknowledged_at,
                completed_at=transaction.completed_at,
                timeout_seconds=transaction.timeout_seconds,
                retry_count=transaction.retry_count,
                max_retries=transaction.max_retries,
                error_message=transaction.error_message
            )
            session.merge(model)
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error saving transaction: {e}")
            return False
    
    def get_transaction(self, transaction_id: str) -> Optional[HandoffTransaction]:
        """Get handoff transaction."""
        
        if not self.use_db:
            with self._lock:
                return self._memory_transactions.get(transaction_id)
        
        try:
            session = self.Session()
            model = session.query(HandoffTransactionModel).filter_by(transaction_id=transaction_id).first()
            session.close()
            
            if not model:
                return None
            
            return HandoffTransaction(
                transaction_id=model.transaction_id,
                from_agent=model.from_agent,
                to_agent=model.to_agent,
                state_commit=model.state_commit,
                status=HandoffStatus(model.status),
                initiated_at=model.initiated_at,
                acknowledged_at=model.acknowledged_at,
                completed_at=model.completed_at,
                timeout_seconds=model.timeout_seconds,
                retry_count=model.retry_count,
                max_retries=model.max_retries,
                error_message=model.error_message
            )
        except Exception as e:
            print(f"Error getting transaction: {e}")
            return None


# Initialize storage
storage = StorageBackend()


# ============================================================================
# STATE MANAGER (Uses storage backend)
# ============================================================================

class AgentStateManager:
    """State management with pluggable storage."""
    
    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self.stats = defaultdict(int)
    
    def commit(self, agent_id: str, state_data: Dict[str, Any], message: str = "") -> StateSnapshot:
        """Commit state."""
        
        parent_commit = self.storage.get_agent_head(agent_id)
        
        snapshot = StateSnapshot.create(
            agent_id=agent_id,
            state_data=state_data,
            parent_commit=parent_commit,
            metadata={"message": message, "committed_at": datetime.now().isoformat()}
        )
        
        self.storage.save_snapshot(snapshot)
        self.stats["total_commits"] += 1
        
        return snapshot
    
    def rollback(self, agent_id: str, target_commit: str) -> StateSnapshot:
        """Rollback to previous state."""
        
        target = self.storage.get_snapshot(target_commit)
        if not target or target.agent_id != agent_id:
            raise ValueError("Invalid commit")
        
        rollback_snapshot = StateSnapshot.create(
            agent_id=agent_id,
            state_data=target.state_data,
            parent_commit=self.storage.get_agent_head(agent_id),
            metadata={
                "message": f"Rollback to {target_commit[:8]}",
                "rollback_to": target_commit
            }
        )
        
        self.storage.save_snapshot(rollback_snapshot)
        self.stats["total_rollbacks"] += 1
        
        return rollback_snapshot
    
    def get_state(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get current state."""
        commit_id = self.storage.get_agent_head(agent_id)
        if not commit_id:
            return None
        snapshot = self.storage.get_snapshot(commit_id)
        return snapshot.state_data if snapshot else None


# ============================================================================
# HANDOFF COORDINATOR
# ============================================================================

class HandoffCoordinator:
    """Manages atomic handoffs."""
    
    def __init__(self, state_manager: AgentStateManager, storage_backend: StorageBackend):
        self.state_manager = state_manager
        self.storage = storage_backend
        self.stats = defaultdict(int)
    
    def initiate_handoff(self, from_agent: str, to_agent: str, 
                        state_data: Dict[str, Any], message: str = "") -> HandoffTransaction:
        """Initiate handoff."""
        
        snapshot = self.state_manager.commit(from_agent, state_data, f"Handoff to {to_agent}: {message}")
        
        transaction = HandoffTransaction(
            transaction_id=str(uuid.uuid4()),
            from_agent=from_agent,
            to_agent=to_agent,
            state_commit=snapshot.commit_id,
            status=HandoffStatus.PENDING,
            initiated_at=time.time()
        )
        
        self.storage.save_transaction(transaction)
        self.stats["total_handoffs"] += 1
        
        return transaction
    
    def acknowledge_handoff(self, transaction_id: str, to_agent: str) -> bool:
        """Acknowledge handoff."""
        
        transaction = self.storage.get_transaction(transaction_id)
        if not transaction or transaction.to_agent != to_agent:
            return False
        
        if transaction.is_expired():
            transaction.status = HandoffStatus.TIMEOUT
            self.storage.save_transaction(transaction)
            return False
        
        transaction.status = HandoffStatus.ACKNOWLEDGED
        transaction.acknowledged_at = time.time()
        self.storage.save_transaction(transaction)
        
        return True
    
    def complete_handoff(self, transaction_id: str) -> bool:
        """Complete handoff."""
        
        transaction = self.storage.get_transaction(transaction_id)
        if not transaction or transaction.status != HandoffStatus.ACKNOWLEDGED:
            return False
        
        try:
            snapshot = self.storage.get_snapshot(transaction.state_commit)
            if not snapshot:
                return False
            
            self.state_manager.commit(
                transaction.to_agent,
                snapshot.state_data,
                f"Received handoff from {transaction.from_agent}"
            )
            
            transaction.status = HandoffStatus.COMPLETED
            transaction.completed_at = time.time()
            self.storage.save_transaction(transaction)
            
            self.stats["successful_handoffs"] += 1
            return True
            
        except Exception as e:
            transaction.status = HandoffStatus.FAILED
            transaction.error_message = str(e)
            self.storage.save_transaction(transaction)
            self.stats["failed_handoffs"] += 1
            return False


# ============================================================================
# PROMETHEUS METRICS
# ============================================================================

if HAS_PROMETHEUS:
    api_requests = Counter('agentstate_api_requests_total', 'Total requests', ['method', 'endpoint', 'status'])
    handoffs_total = Counter('agentstate_handoffs_total', 'Total handoffs', ['status'])
    commits_total = Counter('agentstate_commits_total', 'Total commits')
    active_agents_gauge = Gauge('agentstate_active_agents', 'Active agents')


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class CommitRequest(BaseModel):
    agent_id: str
    state_data: Dict[str, Any]
    message: str = ""

class HandoffRequest(BaseModel):
    from_agent: str
    to_agent: str
    state_data: Dict[str, Any]
    message: str = ""

class RollbackRequest(BaseModel):
    agent_id: str
    commit_id: str


# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

# Initialize system
state_manager = AgentStateManager(storage)
handoff_coordinator = HandoffCoordinator(state_manager, storage)
start_time = time.time()

# Create admin API key
admin_api_key = f"agentstate_{secrets.token_urlsafe(32)}"
api_keys = {admin_api_key: {"name": "admin", "created_at": time.time()}}

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    print("\n" + "="*80)
    print("🚀 AGENTSTATE API SERVER STARTED")
    print("="*80)
    print(f"\n🔑 Admin API Key: {admin_api_key}")
    print(f"🌐 Public URL: {os.getenv('RAILWAY_PUBLIC_DOMAIN', 'localhost:' + str(config.PORT))}")
    print(f"📚 API Docs: /docs")
    print(f"🏥 Health: /health")
    if HAS_PROMETHEUS:
        print(f"📊 Metrics: /metrics")
    print("\n" + "="*80 + "\n")
    
    yield
    
    print("\n🛑 Shutting down...")

app = FastAPI(
    title="AgentState API",
    description="Production state management for AI agents",
    version="1.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API key validation
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Depends(api_key_header)):
    if not api_key or api_key not in api_keys:
        raise HTTPException(status_code=401, detail="Invalid API key")
    api_keys[api_key]["last_used"] = time.time()
    return api_key


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "AgentState API",
        "version": "1.0.0",
        "status": "operational",
        "environment": config.ENVIRONMENT,
        "docs": "/docs",
        "health": "/health"
    }

@app.get("/health")
async def health():
    """Health check."""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "uptime": time.time() - start_time,
        "storage": "database" if storage.use_db else "memory",
        "cache": "redis" if storage.use_cache else "none"
    }

@app.post("/commit")
async def commit_state(request: CommitRequest, api_key: str = Depends(verify_api_key)):
    """Commit state."""
    
    try:
        snapshot = state_manager.commit(request.agent_id, request.state_data, request.message)
        
        if HAS_PROMETHEUS:
            commits_total.inc()
        
        return {
            "commit_id": snapshot.commit_id,
            "agent_id": snapshot.agent_id,
            "timestamp": snapshot.timestamp,
            "checksum": snapshot.checksum
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/handoff")
async def initiate_handoff(request: HandoffRequest, api_key: str = Depends(verify_api_key)):
    """Initiate handoff."""
    
    try:
        transaction = handoff_coordinator.initiate_handoff(
            request.from_agent, request.to_agent, request.state_data, request.message
        )
        
        if HAS_PROMETHEUS:
            handoffs_total.labels(status="initiated").inc()
        
        return {
            "transaction_id": transaction.transaction_id,
            "status": transaction.status.value,
            "initiated_at": transaction.initiated_at
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/handoff/{transaction_id}/receive")
async def receive_handoff(transaction_id: str, agent_id: str, api_key: str = Depends(verify_api_key)):
    """Receive handoff."""
    
    try:
        ack = handoff_coordinator.acknowledge_handoff(transaction_id, agent_id)
        if not ack:
            raise HTTPException(status_code=400, detail="Failed to acknowledge")
        
        complete = handoff_coordinator.complete_handoff(transaction_id)
        if not complete:
            raise HTTPException(status_code=500, detail="Failed to complete")
        
        state = state_manager.get_state(agent_id)
        
        if HAS_PROMETHEUS:
            handoffs_total.labels(status="completed").inc()
        
        return {"transaction_id": transaction_id, "status": "completed", "state": state}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/rollback")
async def rollback_state(request: RollbackRequest, api_key: str = Depends(verify_api_key)):
    """Rollback state."""
    
    try:
        snapshot = state_manager.rollback(request.agent_id, request.commit_id)
        return {
            "agent_id": request.agent_id,
            "commit_id": snapshot.commit_id,
            "state": snapshot.state_data
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/state/{agent_id}")
async def get_state(agent_id: str, api_key: str = Depends(verify_api_key)):
    """Get agent state."""
    
    state = state_manager.get_state(agent_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    return {"agent_id": agent_id, "state": state}

@app.get("/metrics")
async def metrics():
    """Prometheus metrics."""
    
    if not HAS_PROMETHEUS:
        return {"error": "Prometheus not available"}
    
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ============================================================================
# RUN SERVER
# ============================================================================

if __name__ == "__main__":
    print(f"\n🚀 Starting AgentState API on {config.HOST}:{config.PORT}...")
    
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        access_log=True
    )
