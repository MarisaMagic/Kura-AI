from fastapi.exceptions import (
    HTTPException,
    RequestValidationError,
    ResponseValidationError,
)
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from tortoise.exceptions import DoesNotExist, IntegrityError


class SettingNotFound(Exception):
    pass


async def DoesNotExistHandle(req: Request, exc: DoesNotExist) -> JSONResponse:
    return JSONResponse(
        content=dict(code=404, msg="资源不存在"),
        status_code=404,
    )


async def IntegrityHandle(_: Request, exc: IntegrityError) -> JSONResponse:
    return JSONResponse(
        content=dict(code=500, msg="请求无法完成"),
        status_code=500,
    )


async def HttpExcHandle(_: Request, exc: HTTPException) -> JSONResponse:
    content = dict(code=exc.status_code, msg=exc.detail, data=None)
    return JSONResponse(content=content, status_code=exc.status_code)


async def RequestValidationHandle(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = []
    for item in exc.errors():
        errors.append(
            {
                "loc": item.get("loc"),
                "msg": item.get("msg"),
                "type": item.get("type"),
            }
        )
    return JSONResponse(
        content=dict(code=422, msg="请求参数无效", data=errors),
        status_code=422,
    )


async def ResponseValidationHandle(_: Request, exc: ResponseValidationError) -> JSONResponse:
    return JSONResponse(
        content=dict(code=500, msg="请求无法完成"),
        status_code=500,
    )
