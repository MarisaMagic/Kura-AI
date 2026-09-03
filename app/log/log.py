import logging
import sys

from loguru import logger as loguru_logger

from app.settings import settings


class InterceptHandler(logging.Handler):
    """把标准库 logging 记录重定向到 loguru，统一走 loguru sink 输出。"""

    def emit(self, record: logging.LogRecord) -> None:
        # 标准库级别名（WARNING 等）在 loguru 中有同名级别；其余退回数字级别
        try:
            level = loguru_logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 跳过 logging 内部栈帧（handle/callHandlers/_log 等），让 loguru 指向真正产生日志的调用位置
        frame, depth = logging.currentframe(), 1
        if frame is not None:
            frame = frame.f_back
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            depth += 1
            frame = frame.f_back

        loguru_logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


class Loggin:
    def __init__(self) -> None:
        debug = settings.DEBUG
        if debug:
            self.level = "DEBUG"
        else:
            self.level = "INFO"

    def setup_logger(self):
        loguru_logger.remove()
        loguru_logger.add(sink=sys.stdout, level=self.level)

        # stdlib logging（tortoise/uvicorn 等）→ loguru：
        # 根 logger 挂 InterceptHandler，并清掉既有命名 logger 的处理器让其冒泡到根
        logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
        for name in logging.root.manager.loggerDict:
            logging.getLogger(name).handlers = []
            logging.getLogger(name).propagate = True

        # logger.add("my_project.log", level=level, rotation="100 MB")  # 输出到文件（维持现状：仅 stdout）
        return loguru_logger


loggin = Loggin()
logger = loggin.setup_logger()
