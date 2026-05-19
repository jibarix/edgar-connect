"""
Input validation utilities for the EDGAR Financial Tool.
"""

import re

from config.constants import FILING_TYPES


def is_valid_cik(cik):
    """
    Validate if a string is a valid CIK number.

    Args:
        cik (str): CIK number to validate

    Returns:
        bool: True if valid, False otherwise
    """
    if not cik:
        return False

    # Remove any non-digit characters
    cik_digits = re.sub(r'\D', '', cik)

    # CIK should be a 10-digit number (with potential leading zeros)
    return len(cik_digits) <= 10 and cik_digits.isdigit()


def is_valid_filing_type(filing_type):
    """
    Validate if a string is a valid SEC filing type.

    Args:
        filing_type (str): Filing type to validate

    Returns:
        bool: True if valid, False otherwise
    """
    if not filing_type or not isinstance(filing_type, str):
        return False

    # Check if the filing type is in our list of supported types
    return filing_type.upper() in FILING_TYPES
