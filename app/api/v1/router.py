"""
Gom mọi router của v1 thành một.

MẪU THIẾT KẾ: mỗi nhóm chức năng một file router, rồi include vào đây, rồi
include cái này vào app. Ba tầng như vậy giúp:
  - Đổi prefix /api/v1 chỉ sửa 1 chỗ
  - Thêm nhóm mới không phải chạm vào main.py
  - Nhìn file này là biết API có những gì
"""

from fastapi import APIRouter

from app.api.v1 import auth, health, incidents, runbooks, webhooks

# Router gốc của v1. Prefix đặt ở đây, không đặt trong từng file con,
# để các file con không cần biết mình đang ở version nào.
api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(webhooks.router)
api_router.include_router(incidents.router)
api_router.include_router(runbooks.router)

# health không nằm dưới /api/v1 (xem main.py) vì K8s probe thường trỏ vào
# đường dẫn gốc, và health không nên đổi khi API lên v2.
health_router = health.router
