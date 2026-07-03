from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.deps import get_current_user, require_admin
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


@router.post("/review-image", dependencies=[Depends(get_current_user)])
async def upload_review_image(file: UploadFile = File(...)):
    """Any signed-in customer can attach photos to a product review."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    data = await file.read()
    try:
        url = upload_image(data, folder="clothing/reviews")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"url": url}


@router.post("/user-image", dependencies=[Depends(get_current_user)])
async def upload_user_image(file: UploadFile = File(...)):
    """Any signed-in user can upload their profile picture."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    data = await file.read()
    try:
        url = upload_image(data, folder="clothing/users")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"url": url}
