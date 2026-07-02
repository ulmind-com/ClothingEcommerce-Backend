import cloudinary
import cloudinary.uploader

from app.core.config import settings

_configured = False


def _ensure_config() -> bool:
    global _configured
    if _configured:
        return True
    if not (
        settings.CLOUDINARY_CLOUD_NAME
        and settings.CLOUDINARY_API_KEY
        and settings.CLOUDINARY_API_SECRET
    ):
        return False
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )
    _configured = True
    return True


def upload_image(file_bytes: bytes, folder: str = "clothing") -> str:
    if not _ensure_config():
        raise RuntimeError("Cloudinary is not configured")
    result = cloudinary.uploader.upload(file_bytes, folder=folder)
    return result["secure_url"]
