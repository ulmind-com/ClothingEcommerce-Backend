from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.deps import require_admin
from app.services.storage import upload_image

router = APIRouter(prefix="/upload", tags=["upload"])


@router.post("/image", dependencies=[Depends(require_admin)])
async def upload_product_image(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    data = await file.read()
    try:
        url = upload_image(data, folder="clothing/products")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"url": url}
