"""
app.schemas - DTO (Data Transfer Object) bằng Pydantic.

VÌ SAO KHÔNG TRẢ THẲNG MODEL ORM RA API?
  1. BẢO MẬT: model User có hashed_password. Trả thẳng là lộ hash.
  2. HỢP ĐỒNG API: đổi tên cột trong DB không được làm vỡ API của client.
  3. VALIDATE ĐẦU VÀO: Pydantic tự kiểm tra kiểu, độ dài, khoảng giá trị.
  4. TÀI LIỆU: FastAPI đọc schema để sinh OpenAPI/Swagger tự động.

QUY ƯỚC ĐẶT TÊN dùng trong project:
    *In   / *Create / *Update : dữ liệu client GỬI LÊN
    *Out                      : dữ liệu server TRẢ VỀ
"""
