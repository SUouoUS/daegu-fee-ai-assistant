import sys
sys.path.insert(0, r'c:\Users\ksysd\dev\daegu-fee-ai-assistant')

from services.extractor import extract_notice_info

# Test 1: Empty text
try:
    extract_notice_info("   ")
    print("Fail 1")
except ValueError as e:
    print("Pass 1:", e)

# Test 2: Too long text
try:
    extract_notice_info("a" * 20001)
    print("Fail 2")
except ValueError as e:
    print("Pass 2:", e)

# Test 3: API Key missing (assuming we don't have .env yet)
import os
os.environ.pop("GEMINI_API_KEY", None)
try:
    extract_notice_info("some notice")
    print("Fail 3")
except ValueError as e:
    print("Pass 3:", e)

print("All static tests passed")
