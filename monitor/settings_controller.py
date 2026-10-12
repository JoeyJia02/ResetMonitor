"""GUI-independent actions for this project's Windows credential settings.

Only explicit actions access the credential store. Status checks use metadata,
never load saved secrets, and results contain fixed messages without input data.
"""
from monitor.credentials import CredentialInputError, ProjectCredentials
from monitor.credential_store import (
    SMTP_USER, CredentialStoreError, delete_credentials, has_credentials,
    save_credentials,
)


_STORE_MESSAGES = {
    "CREDENTIAL_STORE_UNAVAILABLE": "无法使用 Windows 凭据管理器，请在当前 Windows 用户下打开应用。",
    "CREDENTIAL_STORE_READ_FAILED": "无法检查已保存的凭据，请稍后重试。",
    "CREDENTIAL_STORE_WRITE_FAILED": "凭据保存失败，请稍后重试。",
    "CREDENTIAL_STORE_DELETE_FAILED": "凭据清除失败，请稍后重试。",
    "CREDENTIAL_STORE_INVALID": "项目凭据格式无效，请重新填写三项凭据并保存。",
    "CREDENTIAL_STORE_TOO_LARGE": "凭据内容过长，请检查是否粘贴了其他文字。",
}


class SettingsController:
    """Manage three inputs without retaining secrets on the controller object."""

    def __init__(self, *, saver=None, exists=None, deleter=None):
        self._saver = save_credentials if saver is None else saver
        self._exists = has_credentials if exists is None else exists
        self._deleter = delete_credentials if deleter is None else deleter
        self._stored = None

    def _result(self, success, message):
        return {"success": success, "error": not success,
                "stored": self._stored, "message": message}

    def _failure(self, error, fallback):
        message = (_STORE_MESSAGES.get(error.code, fallback)
                   if isinstance(error, CredentialStoreError) else fallback)
        return self._result(False, message)

    def status(self):
        """Check only whether the single project credential record exists."""
        try:
            present = self._exists()
            if type(present) is not bool:
                raise CredentialStoreError()
            self._stored = present
            message = ("三项凭据已保存，后续运行自动读取。" if present
                       else "尚未保存凭据，请填写下方三项后保存。")
            return self._result(True, message)
        except (Exception, KeyboardInterrupt) as error:
            self._stored = None
            return self._failure(error, "无法检查已保存的凭据，请稍后重试。")

    def save(self, apify, deepseek, qq):
        """Validate and replace the one project record, without reading it."""
        # Strip accidental paste whitespace only at the edges. Internal spaces,
        # control characters, and invalid lengths are rejected without truncation.
        fields = (apify, deepseek, qq)
        if any(not isinstance(value, str) for value in fields):
            return self._result(False, "请填写有效的 Apify Token、DeepSeek Key 和 QQ 邮箱授权码。")
        apify, deepseek, qq = (value.strip() for value in fields)
        if not all((apify, deepseek, qq)):
            return self._result(False, "请填写全部三项：Apify Token、DeepSeek Key 和 QQ 邮箱授权码。")
        if len(qq) != 16:
            return self._result(False, "QQ 邮箱授权码需要 16 位，请粘贴授权码。")
        try:
            credentials = ProjectCredentials(
                apify_token=apify, deepseek_api_key=deepseek,
                smtp_user=SMTP_USER, smtp_password=qq)
        except CredentialInputError:
            return self._result(False, "凭据格式无效，请检查空格、换行、中文或内容长度。")
        try:
            self._saver(credentials)
            self._stored = True
            return self._result(True, "三项凭据已保存，后续运行自动读取。")
        except (Exception, KeyboardInterrupt) as error:
            # A backend failure can leave state uncertain; do not report an old
            # cached status as proof that a replacement succeeded.
            self._stored = None
            return self._failure(error, "凭据保存失败，请稍后重试。")

    def clear(self):
        """Remove only the single project record; missing is a normal outcome."""
        try:
            removed = self._deleter()
            if type(removed) is not bool:
                raise CredentialStoreError()
            self._stored = False
            message = "已清除本项目保存的凭据。" if removed else "本项目没有已保存的凭据。"
            return self._result(True, message)
        except (Exception, KeyboardInterrupt) as error:
            self._stored = None
            return self._failure(error, "凭据清除失败，请稍后重试。")
