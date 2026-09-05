import os
import json
import base64
import logging
from groq import Groq

logger = logging.getLogger(__name__)

DOCUMENT_SCHEMAS = {
    "nbi_clearance": {
        "description": "Philippine NBI Clearance",
        "fields": [
            "full_name",
            "first_name",
            "middle_name",
            "last_name",
            "date_of_birth",
            "clearance_number",
            "date_issued",
            "valid_until",
            "purpose",
        ],
    },
    "police_clearance": {
        "description": "Philippine National Police (PNP) Clearance",
        "fields": [
            "full_name",
            "first_name",
            "middle_name",
            "last_name",
            "date_of_birth",
            "clearance_number",
            "date_issued",
            "valid_until",
            "issuing_office",
        ],
    },
    "national_id_front": {
        "description": "Philippine National ID (PhilSys) - Front side",
        "fields": [
            "full_name",
            "first_name",
            "middle_name",
            "last_name",
            "date_of_birth",
            "philsys_number",
            "address",
        ],
    },
    "national_id_back": {
        "description": "Philippine National ID (PhilSys) - Back side",
        "fields": [
            "philsys_number",
            "blood_type",
            "date_issued",
        ],
    },
}


def get_groq_client() -> Groq | None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.warning("[GroqService] GROQ_API_KEY is not set in environment.")
        return None
    return Groq(api_key=api_key)


def build_system_prompt(document_type: str) -> str:
    schema = DOCUMENT_SCHEMAS.get(document_type, {})
    fields_list = ", ".join(schema.get("fields", ["full_name", "date_issued", "valid_until"]))
    doc_name = schema.get("description", document_type)

    return (
        f"You are a specialized document data extraction AI for the Philippines SerbiSure platform. "
        f"You extract and structure data from {doc_name}.\n"
        f"Extract the following keys:\n{fields_list}\n\n"
        "Rules:\n"
        "1. Return ONLY a single valid JSON object containing the specified keys.\n"
        "2. All dates MUST be in YYYY-MM-DD format (convert from English words or MM/DD/YYYY if necessary). If unavailable or unreadable, return null.\n"
        "3. If any field cannot be found or is blurry, set its value to null. Do not invent or guess.\n"
        "4. Clean and normalize all names to standard casing or UPPERCASE without typos."
    )


def extract_structured_data_from_text(raw_text: str, document_type: str) -> dict:
    """
    Given raw text from Tesseract OCR, use Groq Llama-3.3-70b to sort and structure it into JSON.
    """
    client = get_groq_client()
    if not client:
        return {}

    system_prompt = build_system_prompt(document_type)

    prompt = (
        f"Document Type: {document_type}\n\n"
        f"Raw OCR Extracted Text:\n```\n{raw_text}\n```\n\n"
        "Extract all fields into a single JSON object."
    )

    models_to_try = [
        os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"),
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
    ]

    for model_name in models_to_try:
        try:
            completion = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=600,
                response_format={"type": "json_object"},
            )

            content = completion.choices[0].message.content
            return json.loads(content)
        except Exception as e:
            logger.warning(f"[GroqService] Attempt with model '{model_name}' failed: {e}")
            continue

    logger.error("[GroqService] All models failed in text extraction.")
    return {}


def extract_from_image_vision(image_bytes: bytes, document_type: str) -> tuple[str, dict]:
    """
    Fallback multimodal extraction: uses Groq Vision (llama-3.2-11b-vision-preview)
    directly when Tesseract is not available.
    Returns (raw_visible_text, structured_data_dict).
    """
    client = get_groq_client()
    if not client:
        return "", {}

    b64_data = base64.b64encode(image_bytes).decode("utf-8")
    schema = DOCUMENT_SCHEMAS.get(document_type, {})
    fields_list = ", ".join(schema.get("fields", ["full_name", "date_issued", "valid_until"]))

    prompt = (
        f"You are verifying a Philippine document of type '{document_type}'.\n"
        "First, read all visible printed text on this document verbatim.\n"
        f"Second, extract the structured fields: {fields_list}.\n\n"
        "Return a JSON object with exactly two keys:\n"
        "1. \"raw_text\": A string with all readable text transcribed from the image.\n"
        "2. \"extracted_data\": A JSON dictionary with the extracted fields (dates in YYYY-MM-DD, null if missing)."
    )

    try:
        completion = client.chat.completions.create(
            model="llama-3.2-11b-vision-preview",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_data}",
                            },
                        },
                    ],
                }
            ],
            temperature=0.0,
            max_tokens=800,
            response_format={"type": "json_object"},
        )

        content = completion.choices[0].message.content
        parsed = json.loads(content)
        raw_text = parsed.get("raw_text", "")
        extracted_data = parsed.get("extracted_data", {})
        return raw_text, extracted_data
    except Exception as e:
        logger.error(f"[GroqService] Error in vision extraction: {e}")
        return "", {}
