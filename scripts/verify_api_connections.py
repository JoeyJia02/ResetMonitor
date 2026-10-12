"""Manual, read-only API access check; default execution only previews the plan.

No actor is started, dataset read, model inference requested, or email sent.
Credentials are accepted only through hidden local terminal input.
"""

from decimal import Decimal, InvalidOperation
import getpass
import json
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
import warnings


APIFY_URL = "https://api.apify.com/v2/actors/dYOm3ceq9e9PBnyJA/runs?limit=1&desc=1"
DEEPSEEK_URL = "https://api.deepseek.com/user/balance"
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT_SECONDS = 15
_BALANCE_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")


class _InvalidResponse(ValueError):
    pass


class _HTTPStatus(Exception):
    def __init__(self, code):
        self.code = code


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, "REDIRECT_REFUSED", headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _build_transport():
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # An explicit empty proxy map avoids reading environment proxy settings.
    return build_opener(ProxyHandler({}), _RejectRedirects(), HTTPSHandler(context=context)).open


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidResponse()
        result[key] = value
    return result


def _reject_constant(value):
    raise _InvalidResponse()


def _read_json(url, token, transport):
    request = Request(url, method="GET", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
    })
    with transport(request, timeout=TIMEOUT_SECONDS) as response:
        code = response.getcode()
        if type(code) is not int or code != 200:
            raise _HTTPStatus(code)
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise _InvalidResponse()
    try:
        result = json.loads(body.decode("utf-8"), object_pairs_hook=_strict_object,
                            parse_constant=_reject_constant, parse_float=Decimal)
    except (UnicodeError, ValueError, InvalidOperation):
        raise _InvalidResponse() from None
    if not isinstance(result, dict):
        raise _InvalidResponse()
    return result


def _validate_apify(document):
    data = document.get("data")
    if not isinstance(data, dict):
        raise _InvalidResponse()
    items = data.get("items")
    if not isinstance(items, list) or len(items) > 1 or any(not isinstance(item, dict) for item in items):
        raise _InvalidResponse()


def _validate_deepseek(document):
    available = document.get("is_available")
    balances = document.get("balance_infos")
    if type(available) is not bool or not isinstance(balances, list) or not 1 <= len(balances) <= 2:
        raise _InvalidResponse()
    result = []
    currencies = set()
    for balance in balances:
        if not isinstance(balance, dict):
            raise _InvalidResponse()
        currency, total = balance.get("currency"), balance.get("total_balance")
        if (not isinstance(currency, str) or currency not in {"CNY", "USD"}
                or currency in currencies or not isinstance(total, str)
                or len(total) > 64 or not _BALANCE_NUMBER.fullmatch(total)):
            raise _InvalidResponse()
        try:
            amount = Decimal(total)
        except InvalidOperation:
            raise _InvalidResponse() from None
        if not amount.is_finite() or amount < 0:
            raise _InvalidResponse()
        currencies.add(currency)
        result.append((currency, amount))
    return available, result


def _valid_secret(value):
    return (isinstance(value, str) and 1 <= len(value) <= 4096 and value.isascii()
            and all(33 <= ord(character) <= 126 for character in value))


def _http_code(prefix, code):
    if type(code) is int and 100 <= code <= 599:
        return prefix + "_HTTP_" + str(code)
    return prefix + "_HTTP_ERROR"


def _preview(output):
    print("READ-ONLY API ACCESS CHECK", file=output)
    print("GET " + APIFY_URL, file=output)
    print("GET " + DEEPSEEK_URL, file=output)
    print("At most one GET per service; no retries or redirects; direct HTTPS.", file=output)
    print("No actor start, dataset read, model inference, or email sending.", file=output)
    print("Apify success proves only this GET is accessible; token authentication and Run permission remain unverified.", file=output)
    print("DeepSeek balance success does not validate model inference.", file=output)


def main(argv=None, *, output=None, hidden_input=None, transport=None):
    """Injected dependencies are for offline tests, never CLI switches."""
    output = sys.stdout if output is None else output
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["--preview"], ["--help"], ["--check"]):
        print("API_CHECK_ARGUMENTS_INVALID", file=output)
        return 2
    _preview(output)
    if argv != ["--check"]:
        print("PREVIEW_ONLY", file=output)
        return 0

    try:
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            print("API_CHECK_INTERACTIVE_TERMINAL_REQUIRED", file=output)
            return 2
        hidden_input = getpass.getpass if hidden_input is None else hidden_input
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            apify_token = hidden_input("Apify project token (hidden): ")
            deepseek_key = hidden_input("DeepSeek API key (hidden): ")
        if not _valid_secret(apify_token) or not _valid_secret(deepseek_key):
            print("API_CHECK_CREDENTIAL_INVALID", file=output)
            return 2
        transport = _build_transport() if transport is None else transport
        all_ok = True
        for prefix, url, token in (
                ("APIFY", APIFY_URL, apify_token),
                ("DEEPSEEK", DEEPSEEK_URL, deepseek_key)):
            try:
                document = _read_json(url, token, transport)
                if prefix == "APIFY":
                    _validate_apify(document)
                    print("APIFY_LIST_RUNS_OK", file=output)
                else:
                    available, balances = _validate_deepseek(document)
                    print("DEEPSEEK_BALANCE_OK", file=output)
                    print("is_available=" + ("true" if available else "false"), file=output)
                    for currency, amount in balances:
                        print("currency=" + currency + " total_balance=" + str(amount), file=output)
            except HTTPError as error:
                code = error.code
                try:
                    error.close()
                except Exception:
                    pass
                print(_http_code(prefix, code), file=output)
                all_ok = False
            except _HTTPStatus as error:
                print(_http_code(prefix, error.code), file=output)
                all_ok = False
            except (URLError, OSError):
                print(prefix + "_NETWORK_ERROR", file=output)
                all_ok = False
            except _InvalidResponse:
                print(prefix + "_RESPONSE_INVALID", file=output)
                all_ok = False
            except Exception:
                print(prefix + "_LOCAL_ERROR", file=output)
                all_ok = False
        return 0 if all_ok else 1
    except getpass.GetPassWarning:
        print("API_CHECK_HIDDEN_INPUT_REQUIRED", file=output)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("API_CHECK_CANCELLED", file=output)
        return 2
    except Exception:
        print("API_CHECK_LOCAL_ERROR", file=output)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
