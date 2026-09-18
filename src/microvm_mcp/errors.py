class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status

    def body(self):
        return {"error": {"code": self.code, "message": str(self)}}
