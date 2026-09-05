import os
from google import genai
from google.genai import types
from pydantic import BaseModel

class NoticeInfo(BaseModel):
    title: str | None
    agency: str | None
    amount: int | None
    due_date: str | None
    payment_method: str | None

def test_genai():
    # just testing if the code structure compiles and runs with dummy api key
    # we expect an authentication error, which is fine to verify the SDK usage
    try:
        client = genai.Client(api_key="DUMMY")
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents="test",
            config=types.GenerateContentConfig(
                system_instruction="You are an assistant.",
                response_mime_type="application/json",
                response_schema=NoticeInfo,
            ),
        )
        print("Success")
    except Exception as e:
        print(f"Error: {type(e).__name__} - {e}")

if __name__ == "__main__":
    test_genai()
