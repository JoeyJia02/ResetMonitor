"""Save, update, inspect or remove this project's Windows-managed credentials."""

import getpass
from pathlib import Path
import sys
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.local_settings import SENDER as LOCAL_SENDER
from monitor.credentials import CredentialInputError, ProjectCredentials
from monitor.credential_store import (
    CredentialStoreError, delete_credentials, has_credentials,
    load_credentials, save_credentials,
)


SENDER = LOCAL_SENDER


def prompt_credentials(hidden_input=None):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise CredentialInputError()
    hidden_input = getpass.getpass if hidden_input is None else hidden_input
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = hidden_input("Apify project token (hidden): ")
            key = hidden_input("DeepSeek API key (hidden): ")
            password = hidden_input("QQ SMTP authorization code (hidden): ")
        credentials = ProjectCredentials(apify_token=token, deepseek_api_key=key,
                                         smtp_user=SENDER, smtp_password=password)
        if len(password) != 16:
            raise CredentialInputError()
        return credentials
    except (Exception, KeyboardInterrupt):
        raise CredentialInputError() from None


def get_saved_or_prompt_credentials(*, hidden_input=None, loader=None, saver=None, output=None):
    loader = load_credentials if loader is None else loader
    saver = save_credentials if saver is None else saver
    credentials = loader()
    if credentials is not None:
        if not isinstance(credentials, ProjectCredentials):
            raise CredentialInputError()
        if output is not None:
            print("已自动读取本机保存的项目凭据。", file=output)
        return credentials
    credentials = prompt_credentials(hidden_input)
    saver(credentials)
    if output is not None:
        print("三项凭据已保存到 Windows 凭据管理器，后续自动读取。", file=output)
    return credentials


def main(argv=None, *, output=None, hidden_input=None, saver=None, exists=None, deleter=None):
    output = sys.stdout if output is None else output
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["--help"], ["--save"], ["--status"], ["--delete"]):
        print("CREDENTIAL_ARGUMENTS_INVALID", file=output)
        return 2
    if argv in ([], ["--help"]):
        print("--save 首次保存或更新三项凭据；--status 查看是否已保存；--delete 清除项目凭据。", file=output)
        print("保存到当前用户的 Windows 凭据管理器；此入口只管理凭据。", file=output)
        return 0
    try:
        if argv == ["--save"]:
            credentials = prompt_credentials(hidden_input)
            (save_credentials if saver is None else saver)(credentials)
            print("CREDENTIALS_SAVED：后续运行自动读取。", file=output)
        elif argv == ["--status"]:
            present = (has_credentials if exists is None else exists)()
            print("CREDENTIALS_STORED" if present else "CREDENTIALS_NOT_STORED", file=output)
        else:
            removed = (delete_credentials if deleter is None else deleter)()
            print("CREDENTIALS_DELETED" if removed else "CREDENTIALS_NOT_STORED", file=output)
        return 0
    except CredentialStoreError as error:
        print(error.code, file=output)
    except CredentialInputError:
        print("PROJECT_CREDENTIAL_INPUT_REQUIRED", file=output)
    except (KeyboardInterrupt, EOFError):
        print("CREDENTIALS_CANCELLED", file=output)
    except Exception:
        print("CREDENTIALS_LOCAL_ERROR", file=output)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
