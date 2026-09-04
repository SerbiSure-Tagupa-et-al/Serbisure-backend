import os
import shutil
import base64
import logging
from io import BytesIO
import requests
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
import cloudinary.utils

logger = logging.getLogger(__name__)

GOOGLE_VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"

# Auto-detect Tesseract binary location across Windows, Linux, and custom env
DEFAULT_WINDOWS_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"C:\Users\admin\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
]


def is_google_vision_available() -> bool:
    """Check if Google Cloud Vision API key is configured."""
    return bool(os.getenv("GOOGLE_VISION_API_KEY"))


def extract_text_with_google_vision(image_bytes: bytes) -> str:
    """
    Extract text using Google Cloud Vision API (DOCUMENT_TEXT_DETECTION).
    This handles dense text, complex layouts, small fonts, stamps, and watermarks
    typical of Philippine NBI, Police clearances, and PhilSys National IDs.
    """
    api_key = os.getenv("GOOGLE_VISION_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_VISION_API_KEY is not set in environment.")

    # Encode image bytes to base64 string
    base64_content = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "requests": [
            {
                "image": {
                    "content": base64_content
                },
                "features": [
                    {
                        "type": "DOCUMENT_TEXT_DETECTION",
                        "maxResults": 1
                    }
                ],
                "imageContext": {
                    "languageHints": ["en", "fil"]
                }
            }
        ]
    }

    response = requests.post(
        f"{GOOGLE_VISION_API_URL}?key={api_key}",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30
    )

    if not response.ok:
        logger.error(f"[GoogleVision] API request failed ({response.status_code}): {response.text}")
        response.raise_for_status()

    result = response.json()
    responses = result.get("responses", [])
    if not responses:
        return ""

    first_resp = responses[0]
    if "error" in first_resp:
        err_msg = first_resp["error"].get("message", "Google Vision error")
        logger.error(f"[GoogleVision] Response error: {err_msg}")
        raise RuntimeError(err_msg)

    # fullTextAnnotation provides the cleanest formatted text for documents
    full_text_annotation = first_resp.get("fullTextAnnotation")
    if full_text_annotation and full_text_annotation.get("text"):
        return full_text_annotation["text"].strip()

    # Fallback to textAnnotations[0]
    text_annotations = first_resp.get("textAnnotations", [])
    if text_annotations and text_annotations[0].get("description"):
        return text_annotations[0]["description"].strip()

    return ""


def configure_tesseract():
    """Configure tesseract command path if not already in system PATH."""
    custom_cmd = os.getenv("TESSERACT_CMD")
    if custom_cmd and os.path.exists(custom_cmd):
        pytesseract.pytesseract.tesseract_cmd = custom_cmd
        return True

    if shutil.which("tesseract"):
        return True

    for p in DEFAULT_WINDOWS_PATHS:
        if os.path.exists(p):
            pytesseract.pytesseract.tesseract_cmd = p
            return True

    return False


def is_tesseract_available() -> bool:
    """Check if Tesseract OCR executable is available."""
    try:
        configure_tesseract()
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def fetch_image_from_cloudinary(public_id: str) -> tuple[Image.Image, bytes]:
    """
    Generate signed URL for authenticated Cloudinary asset and return Pillow Image + raw bytes.
    """
    url, _ = cloudinary.utils.cloudinary_url(
        public_id,
        type="authenticated",
        sign_url=True,
    )

    response = requests.get(url, timeout=30)
    response.raise_for_status()

    raw_bytes = response.content
    img = Image.open(BytesIO(raw_bytes))
    return img, raw_bytes


def preprocess_image_for_ocr(image: Image.Image) -> Image.Image:
    """
    Enhance document image to improve OCR readability:
    - Convert to grayscale
    - Boost contrast
    - Sharpen slightly
    """
    gray = image.convert("L")

    w, h = gray.size
    if w < 1000 or h < 1000:
        gray = gray.resize((w * 2, h * 2), Image.Resampling.LANCZOS)

    enhancer = ImageEnhance.Contrast(gray)
    contrasted = enhancer.enhance(1.8)

    sharpened = contrasted.filter(ImageFilter.SHARPEN)
    return sharpened


def extract_text_with_tesseract(image: Image.Image) -> str:
    """
    Run Tesseract OCR on a PIL Image.
    """
    if not configure_tesseract():
        raise RuntimeError("Tesseract binary not found on this system.")

    processed = preprocess_image_for_ocr(image)
    custom_config = r"--oem 3 --psm 6"
    text = pytesseract.image_to_string(processed, lang="eng", config=custom_config)
    return text.strip()
