"""Chilean RUT validation and formatting utilities."""

import re
from typing import Optional


def clean_rut(rut: str) -> str:
    """
    Clean RUT removing dots and spaces.
    
    Returns RUT in format: 12345678-9
    """
    # Remove dots, spaces, and extra characters
    cleaned = re.sub(r"[.\s]", "", rut.strip().upper())
    
    # Ensure hyphen before verification digit
    if "-" not in cleaned and len(cleaned) > 1:
        cleaned = cleaned[:-1] + "-" + cleaned[-1]
    
    return cleaned


def validate_rut(rut: str) -> bool:
    """
    Validate Chilean RUT using verification digit algorithm.
    
    Args:
        rut: RUT in any format (with or without dots/hyphen)
    
    Returns:
        True if RUT is valid
    """
    cleaned = clean_rut(rut)
    
    # Check format
    match = re.match(r"^(\d{7,8})-?([\dkK])$", cleaned)
    if not match:
        return False
    
    number, provided_digit = match.groups()
    provided_digit = provided_digit.upper()
    
    # Calculate verification digit
    calculated_digit = calculate_verification_digit(int(number))
    
    return provided_digit == calculated_digit


def calculate_verification_digit(rut_number: int) -> str:
    """
    Calculate RUT verification digit.
    
    Args:
        rut_number: RUT number without verification digit
    
    Returns:
        Verification digit (0-9 or K)
    """
    suma = 0
    multiplicador = 2
    
    for digit in reversed(str(rut_number)):
        suma += int(digit) * multiplicador
        multiplicador = multiplicador + 1 if multiplicador < 7 else 2
    
    resto = suma % 11
    resultado = 11 - resto
    
    if resultado == 11:
        return "0"
    elif resultado == 10:
        return "K"
    else:
        return str(resultado)


def format_rut(rut: str, with_dots: bool = True) -> Optional[str]:
    """
    Format RUT for display.
    
    Args:
        rut: RUT in any format
        with_dots: Include thousands separators
    
    Returns:
        Formatted RUT or None if invalid
    """
    cleaned = clean_rut(rut)
    
    if not validate_rut(cleaned):
        return None
    
    # Split number and digit
    parts = cleaned.split("-")
    if len(parts) != 2:
        return None
    
    number, digit = parts
    
    if with_dots:
        # Add thousands separators
        formatted_number = ""
        for i, char in enumerate(reversed(number)):
            if i > 0 and i % 3 == 0:
                formatted_number = "." + formatted_number
            formatted_number = char + formatted_number
        return f"{formatted_number}-{digit}"
    else:
        return f"{number}-{digit}"
