"""Helper functions for the EDGAR Financial Tool."""
from __future__ import annotations

import time
import logging
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


def retry_request(request_func, *args, max_retries: int = 3, retry_delay: int = 1, **kwargs):
    """Retry an httpx request callable with exponential backoff.

    Honors HTTP 429 Retry-After. Raises httpx.HTTPError if all retries fail.
    """
    last_exception: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = request_func(*args, **kwargs)

            if response.status_code == 429:
                wait_time = int(response.headers.get('Retry-After', retry_delay * 2))
                logger.warning(f"Rate limited. Waiting {wait_time} seconds.")
                time.sleep(wait_time)
                continue

            return response

        except httpx.HTTPError as e:
            last_exception = e

            if attempt >= max_retries:
                logger.error(f"Request failed after {max_retries + 1} attempts: {e}")
                raise

            backoff_time = retry_delay * (2 ** attempt)
            logger.warning(f"Request attempt {attempt + 1} failed: {e}. "
                           f"Retrying in {backoff_time} seconds...")
            time.sleep(backoff_time)

    if last_exception:
        raise last_exception
    raise httpx.HTTPError("All request attempts failed")


def format_financial_number(number, decimals=0, use_commas=True, use_scaling=False):
    """
    Format a financial number with appropriate formatting.
    
    Args:
        number (float): The number to format
        decimals (int): Number of decimal places (default: 0 for no decimals)
        use_commas (bool): Whether to use thousand separators
        use_scaling (bool): Whether to scale with K, M, B suffixes
        
    Returns:
        str: Formatted number
    """
    if number is None:
        return "N/A"
        
    abs_number = abs(number)
    
    if use_scaling:
        # Use scaling (K, M, B) if requested
        if abs_number >= 1_000_000_000:
            return f"{number / 1_000_000_000:.{decimals}f}B"
        elif abs_number >= 1_000_000:
            return f"{number / 1_000_000:.{decimals}f}M"
        elif abs_number >= 1_000:
            return f"{number / 1_000:.{decimals}f}K"
    
    # Format with commas if requested
    if use_commas:
        return f"{number:,.{decimals}f}"
    else:
        return f"{number:.{decimals}f}"


def parse_date(date_str):
    """
    Parse a date string into a datetime object.
    
    Args:
        date_str (str): Date string in various formats
        
    Returns:
        datetime: Parsed datetime object or None if parsing fails
    """
    if not date_str:
        return None
        
    date_formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%Y%m%d"
    ]
    
    for fmt in date_formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    
    return None