"""
Tests for SessionStore session validation caching.

Tests:
1. is_session_valid_cached returns None when not cached
2. cache_session_validity stores valid/invalid status
3. is_session_valid_cached returns cached value
4. invalidate_session_cache clears cache
5. Cache works with real Redis (integration test)
"""

import pytest
from unittest.mock import MagicMock, patch


class TestSessionValidationCache:
    """Test session validation caching methods."""
    
    def test_is_session_valid_cached_returns_none_when_not_cached(self):
        """is_session_valid_cached returns None when not in cache."""
        with patch('app.services.session_store.get_redis_client') as mock_redis:
            mock_client = MagicMock()
            mock_client.get.return_value = None
            mock_redis.return_value = mock_client
            
            from app.services.session_store import SessionStore
            
            store = SessionStore()
            result = store.is_session_valid_cached("session-123")
            
            assert result is None
            mock_client.get.assert_called_once()
    
    def test_is_session_valid_cached_returns_true_when_valid(self):
        """is_session_valid_cached returns True when cached as valid."""
        with patch('app.services.session_store.get_redis_client') as mock_redis:
            mock_client = MagicMock()
            mock_client.get.return_value = "valid"
            mock_redis.return_value = mock_client
            
            from app.services.session_store import SessionStore
            
            store = SessionStore()
            result = store.is_session_valid_cached("session-123")
            
            assert result is True
    
    def test_is_session_valid_cached_returns_false_when_invalid(self):
        """is_session_valid_cached returns False when cached as invalid."""
        with patch('app.services.session_store.get_redis_client') as mock_redis:
            mock_client = MagicMock()
            mock_client.get.return_value = "invalid"
            mock_redis.return_value = mock_client
            
            from app.services.session_store import SessionStore
            
            store = SessionStore()
            result = store.is_session_valid_cached("session-123")
            
            assert result is False
    
    def test_cache_session_validity_stores_valid(self):
        """cache_session_validity stores 'valid' for True."""
        with patch('app.services.session_store.get_redis_client') as mock_redis:
            mock_client = MagicMock()
            mock_redis.return_value = mock_client
            
            from app.services.session_store import (
                SessionStore,
                SESSION_VALIDATION_CACHE_TTL,
            )
            
            store = SessionStore()
            result = store.cache_session_validity("session-123", True)
            
            assert result is True
            mock_client.setex.assert_called_once()
            
            # Check the call args
            call_args = mock_client.setex.call_args
            assert "pjud:session_valid:session-123" in call_args[0][0]
            assert call_args[0][1] == SESSION_VALIDATION_CACHE_TTL
            assert call_args[0][2] == "valid"
    
    def test_cache_session_validity_stores_invalid(self):
        """cache_session_validity stores 'invalid' for False."""
        with patch('app.services.session_store.get_redis_client') as mock_redis:
            mock_client = MagicMock()
            mock_redis.return_value = mock_client
            
            from app.services.session_store import SessionStore
            
            store = SessionStore()
            store.cache_session_validity("session-123", False)
            
            call_args = mock_client.setex.call_args
            assert call_args[0][2] == "invalid"
    
    def test_invalidate_session_cache_deletes_key(self):
        """invalidate_session_cache deletes the cache key."""
        with patch('app.services.session_store.get_redis_client') as mock_redis:
            mock_client = MagicMock()
            mock_redis.return_value = mock_client
            
            from app.services.session_store import SessionStore
            
            store = SessionStore()
            result = store.invalidate_session_cache("session-123")
            
            assert result is True
            mock_client.delete.assert_called_once()
            
            call_args = mock_client.delete.call_args
            assert "pjud:session_valid:session-123" in call_args[0][0]
    
    def test_cache_methods_handle_redis_unavailable(self):
        """Cache methods handle Redis being unavailable."""
        with patch('app.services.session_store.get_redis_client') as mock_redis:
            mock_redis.return_value = None  # Redis unavailable
            
            from app.services.session_store import SessionStore
            
            store = SessionStore()
            
            # All methods should return gracefully
            assert store.is_session_valid_cached("session-123") is None
            assert store.cache_session_validity("session-123", True) is False
            assert store.invalidate_session_cache("session-123") is False


class TestSessionValidationCacheIntegration:
    """Integration tests for session validation cache with real Redis."""
    
    @pytest.mark.integration
    def test_cache_roundtrip(self):
        """Full cache roundtrip: cache, read, invalidate."""
        from app.core.redis import get_redis_client
        from app.services.session_store import SessionStore
        
        redis = get_redis_client()
        if redis is None:
            pytest.skip("Redis not available")
        
        store = SessionStore()
        session_id = "test-session-integration"
        
        try:
            # Initially not cached
            assert store.is_session_valid_cached(session_id) is None
            
            # Cache as valid
            assert store.cache_session_validity(session_id, True) is True
            assert store.is_session_valid_cached(session_id) is True
            
            # Invalidate
            assert store.invalidate_session_cache(session_id) is True
            assert store.is_session_valid_cached(session_id) is None
            
            # Cache as invalid
            assert store.cache_session_validity(session_id, False) is True
            assert store.is_session_valid_cached(session_id) is False
            
        finally:
            # Cleanup
            store.invalidate_session_cache(session_id)
