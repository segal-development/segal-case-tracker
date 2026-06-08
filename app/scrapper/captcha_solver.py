"""2Captcha integration for solving reCAPTCHA v3 on PJUD."""

import asyncio
import logging
from typing import Optional

import httpx

from app.config import settings


logger = logging.getLogger(__name__)


class CaptchaSolver:
    """
    Solve PJUD reCAPTCHA using 2Captcha service.
    
    PJUD uses reCAPTCHA v3 (invisible) with:
    - Sitekey: 6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt
    - Action: validate_captcha_seg_clave_hn
    
    2Captcha pricing: ~$2.99 per 1000 solves
    """
    
    API_URL = "https://2captcha.com"
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize captcha solver.
        
        Args:
            api_key: 2Captcha API key. If None, uses settings.CAPTCHA_API_KEY
        """
        self.api_key = api_key or settings.CAPTCHA_API_KEY
    
    async def solve_recaptcha_v3(
        self,
        sitekey: str,
        page_url: str,
        action: str = "verify",
        min_score: float = 0.7,
        timeout: int = 120,
    ) -> str:
        """
        Solve reCAPTCHA v3 (invisible).
        
        Args:
            sitekey: Google reCAPTCHA site key
            page_url: URL where captcha appears
            action: reCAPTCHA action name (e.g., "validate_captcha_seg_clave_hn")
            min_score: Minimum score required (0.1 to 0.9)
            timeout: Max wait time in seconds
        
        Returns:
            Captcha response token
        
        Raises:
            CaptchaError: If solving fails
        """
        if not self.api_key:
            raise CaptchaError("CAPTCHA_API_KEY not configured")
        
        logger.info(f"Solving reCAPTCHA v3 for {page_url} action={action}")
        
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Submit captcha task
            submit_response = await client.post(
                f"{self.API_URL}/in.php",
                data={
                    "key": self.api_key,
                    "method": "userrecaptcha",
                    "version": "v3",
                    "googlekey": sitekey,
                    "pageurl": page_url,
                    "action": action,
                    "min_score": min_score,
                    "json": 1,
                },
            )
            submit_data = submit_response.json()
            
            if submit_data.get("status") != 1:
                error = submit_data.get("request", "Unknown error")
                raise CaptchaError(f"Failed to submit captcha: {error}")
            
            captcha_id = submit_data["request"]
            logger.debug(f"Captcha submitted, ID: {captcha_id}")
            
            # 2. Poll for result
            poll_interval = 5
            max_polls = timeout // poll_interval
            
            for i in range(max_polls):
                await asyncio.sleep(poll_interval)
                
                result_response = await client.get(
                    f"{self.API_URL}/res.php",
                    params={
                        "key": self.api_key,
                        "action": "get",
                        "id": captcha_id,
                        "json": 1,
                    },
                )
                result_data = result_response.json()
                
                if result_data.get("status") == 1:
                    token = result_data["request"]
                    logger.info(f"Captcha solved successfully ({i * poll_interval}s)")
                    return token
                
                request_status = result_data.get("request", "")
                
                if request_status == "CAPCHA_NOT_READY":
                    logger.debug(f"Captcha not ready, polling... ({i+1}/{max_polls})")
                    continue
                
                # Any other status is an error
                raise CaptchaError(f"Captcha solving failed: {request_status}")
            
            raise CaptchaError(f"Captcha solving timed out after {timeout}s")
    
    async def solve_recaptcha_v2(
        self,
        sitekey: str,
        page_url: str,
        timeout: int = 120,
    ) -> str:
        """
        Solve reCAPTCHA v2 (checkbox or invisible).
        
        Args:
            sitekey: Google reCAPTCHA site key
            page_url: URL where captcha appears
            timeout: Max wait time in seconds
        
        Returns:
            Captcha response token
        
        Raises:
            CaptchaError: If solving fails
        """
        if not self.api_key:
            raise CaptchaError("CAPTCHA_API_KEY not configured")
        
        logger.info(f"Solving reCAPTCHA v2 for {page_url}")
        
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Submit captcha task
            submit_response = await client.post(
                f"{self.API_URL}/in.php",
                data={
                    "key": self.api_key,
                    "method": "userrecaptcha",
                    "googlekey": sitekey,
                    "pageurl": page_url,
                    "json": 1,
                },
            )
            submit_data = submit_response.json()
            
            if submit_data.get("status") != 1:
                error = submit_data.get("request", "Unknown error")
                raise CaptchaError(f"Failed to submit captcha: {error}")
            
            captcha_id = submit_data["request"]
            logger.debug(f"Captcha submitted, ID: {captcha_id}")
            
            # 2. Poll for result
            poll_interval = 5
            max_polls = timeout // poll_interval
            
            for i in range(max_polls):
                await asyncio.sleep(poll_interval)
                
                result_response = await client.get(
                    f"{self.API_URL}/res.php",
                    params={
                        "key": self.api_key,
                        "action": "get",
                        "id": captcha_id,
                        "json": 1,
                    },
                )
                result_data = result_response.json()
                
                if result_data.get("status") == 1:
                    token = result_data["request"]
                    logger.info(f"Captcha solved successfully ({i * poll_interval}s)")
                    return token
                
                if result_data.get("request") == "CAPCHA_NOT_READY":
                    continue
                
                raise CaptchaError(f"Captcha solving failed: {result_data.get('request')}")
            
            raise CaptchaError(f"Captcha solving timed out after {timeout}s")
    
    async def get_balance(self) -> float:
        """
        Get current 2Captcha account balance.
        
        Returns:
            Balance in USD
        """
        if not self.api_key:
            raise CaptchaError("CAPTCHA_API_KEY not configured")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.API_URL}/res.php",
                params={
                    "key": self.api_key,
                    "action": "getbalance",
                    "json": 1,
                },
            )
            data = response.json()
            
            if data.get("status") == 1:
                return float(data["request"])
            
            raise CaptchaError(f"Failed to get balance: {data.get('request')}")
    
    async def report_bad(self, captcha_id: str) -> bool:
        """
        Report a bad captcha (for refund).
        
        Call this if a captcha solution didn't work.
        
        Returns:
            True if report was accepted
        """
        if not self.api_key:
            return False
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.API_URL}/res.php",
                params={
                    "key": self.api_key,
                    "action": "reportbad",
                    "id": captcha_id,
                    "json": 1,
                },
            )
            data = response.json()
            return data.get("status") == 1


class CaptchaError(Exception):
    """Raised when captcha solving fails."""
    pass
