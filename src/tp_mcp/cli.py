"""CLI commands for TrainingPeaks MCP Server."""

import asyncio
import getpass
import sys

from tp_mcp.auth import (
    AuthStatus,
    clear_credential,
    get_credential,
    get_storage_backend,
    is_keyring_available,
    store_credential,
    validate_auth_sync,
)
from tp_mcp.auth import tp_login as tpl
from tp_mcp.auth.browser import extract_tp_cookie


def _finalize_cookie(cookie: str) -> int:
    """Validate and store a cookie; print a summary. Returns an exit code."""
    print()
    print("Validating...")
    result = validate_auth_sync(cookie)
    if not result.is_valid:
        print(f"Error: {result.message}")
        if result.status == AuthStatus.EXPIRED:
            print("The cookie may have expired. Please get a fresh cookie.")
        elif result.status == AuthStatus.INVALID:
            print("The cookie appears to be invalid. Check that you copied it correctly.")
        return 1

    store_result = store_credential(cookie)
    if not store_result.success:
        print(f"Error storing credential: {store_result.message}")
        return 1

    print()
    print("Authentication successful!")
    print(f"  Email: {result.email}")
    print(f"  Athlete ID: {result.athlete_id}")
    print()
    print("You can now use 'tp-mcp serve' to start the MCP server.")
    return 0


async def _password_login() -> str | None:
    """Interactive email/password (+ MFA) login. Returns a cookie or None."""
    username = input("TrainingPeaks username or email: ").strip()
    password = getpass.getpass("Password (hidden): ")

    result = await tpl.login(username, password)

    if result.outcome == tpl.LoginOutcome.MFA_REQUIRED and result.mfa_state:
        methods = result.mfa_state.available_methods
        if methods:
            print(f"Multi-factor authentication required (methods: {', '.join(methods)}).")
        else:
            print("Multi-factor authentication required.")
        code = getpass.getpass("Enter verification code (hidden): ")
        result = await tpl.submit_mfa(result.mfa_state, code)

    if result.outcome == tpl.LoginOutcome.SUCCESS and result.cookie:
        return result.cookie

    print(f"Error: {result.message}")
    if result.outcome == tpl.LoginOutcome.CAPTCHA_REQUIRED:
        print("Tip: use 'tp-mcp auth --paste' after copying the cookie from your browser.")
    return None


def cmd_auth(from_browser: str | None = None, paste: bool = False) -> int:
    """Interactive authentication flow.

    Default: email/password (+ MFA) sign-in via TrainingPeaks. Legacy modes:
    ``--from-browser`` extracts the cookie from a local browser; ``--paste``
    accepts a manually copied cookie.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    print("TrainingPeaks MCP Authentication")
    print("=" * 40)
    print()

    if not is_keyring_available():
        print("Warning: No system keyring available.")
        print("Cookie will be stored in an encrypted file.")
        print()

    # Check for an existing valid credential first.
    existing = get_credential()
    if existing.success and existing.cookie:
        result = validate_auth_sync(existing.cookie)
        if result.is_valid:
            print(f"Already authenticated as: {result.email} (athlete {result.athlete_id})")
            if input("Re-authenticate? [y/N]: ").strip().lower() != "y":
                return 0

    # Legacy: extract from browser.
    if from_browser:
        print(f"Extracting cookie from {from_browser}... (legacy mode)")
        browser_result = extract_tp_cookie(from_browser if from_browser != "auto" else None)
        if not browser_result.success:
            print(f"Error: {browser_result.message}")
            return 1
        print(f"Found cookie in {browser_result.browser}")
        return _finalize_cookie(browser_result.cookie)

    # Legacy: manual cookie paste.
    if paste:
        print("Paste the Production_tpAuth cookie from your browser (legacy mode).")
        print("  app.trainingpeaks.com -> DevTools (F12) -> Application -> Cookies")
        try:
            cookie = getpass.getpass("Paste cookie value (hidden): ")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 1
        if not cookie.strip():
            print("Error: No cookie provided.")
            return 1
        return _finalize_cookie(cookie.strip())

    # Default: email/password (+ MFA) sign-in.
    try:
        login_cookie = asyncio.run(_password_login())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 1
    if not login_cookie:
        return 1
    return _finalize_cookie(login_cookie)


def cmd_auth_status() -> int:
    """Check current authentication status.

    Returns:
        Exit code (0 for authenticated, 1 for not authenticated).
    """
    cred = get_credential()
    if not cred.success or not cred.cookie:
        print("Not authenticated.")
        print("Run 'tp-mcp auth' to authenticate.")
        return 1

    print("Checking authentication status...")
    result = validate_auth_sync(cred.cookie)

    if result.is_valid:
        print("Authenticated")
        print(f"  Email: {result.email}")
        print(f"  Athlete ID: {result.athlete_id}")
        print(f"  Storage: {get_storage_backend()}")
        return 0
    else:
        print(f"Authentication invalid: {result.message}")
        print("Run 'tp-mcp auth' to re-authenticate.")
        return 1


def cmd_auth_clear() -> int:
    """Clear stored credentials.

    Returns:
        Exit code (0 for success).
    """
    result = clear_credential()
    if result.success:
        print("Credentials cleared.")
    else:
        print(f"Note: {result.message}")
    return 0


def cmd_serve() -> int:
    """Start the MCP server.

    Returns:
        Exit code.
    """
    from tp_mcp.server import run_server

    return run_server()


def cmd_serve_http() -> int:
    """Start the MCP server over Streamable HTTP (for hosted/remote use).

    Listens on $PORT (default 8000) and serves the MCP endpoint at /mcp.
    Intended for platforms like Railway. Auth uses the TP_AUTH_COOKIE env var.

    Returns:
        Exit code.
    """
    from tp_mcp.http_server import run_http_server

    return run_http_server()


def cmd_config() -> int:
    """Output Claude Desktop config snippet.

    Returns:
        Exit code (0).
    """
    import json
    import shutil

    # Find the tp-mcp binary path
    tp_mcp_path = shutil.which("tp-mcp")
    if not tp_mcp_path:
        # Fall back to sys.executable directory
        from pathlib import Path
        tp_mcp_path = str(Path(sys.executable).parent / "tp-mcp")

    config = {
        "trainingpeaks": {
            "command": tp_mcp_path,
            "args": ["serve"]
        }
    }

    print("Add this to your Claude Desktop config inside \"mcpServers\": {}")
    print()
    print(json.dumps(config, indent=2))
    return 0


def cmd_help() -> int:
    """Show help message.

    Returns:
        Exit code (0).
    """
    print("TrainingPeaks MCP Server")
    print()
    print("Usage: tp-mcp <command> [options]")
    print()
    print("Commands:")
    print("  auth                  Sign in with your TrainingPeaks email & password (+ MFA)")
    print("    --from-browser X    Legacy: extract cookie from browser (chrome, firefox, safari, edge, auto)")
    print("    --paste             Legacy: paste a Production_tpAuth cookie manually")
    print("  auth-status           Check authentication status")
    print("  auth-clear            Clear stored cookie")
    print("  config                Output Claude Desktop config snippet")
    print("  serve                 Start the MCP server (stdio, for local clients)")
    print("  serve-http            Start the MCP server over HTTP (for hosting, e.g. Railway)")
    print("  help                  Show this help message")
    print()
    print("Examples:")
    print("  tp-mcp auth                      # Email/password sign-in")
    print("  tp-mcp auth --paste              # Paste a cookie (if sign-in is blocked)")
    print("  tp-mcp auth --from-browser auto  # Auto-detect browser")
    print()
    print("Hosted (HTTP) sign-in env vars: TP_MCP_PUBLIC_URL, TP_MCP_TOKEN_SECRET,")
    print("  TP_MCP_DB_PATH, TP_MCP_ENC_KEY (see DEPLOY_RAILWAY.md).")
    print()
    return 0


def main() -> int:
    """Main CLI entry point.

    Returns:
        Exit code.
    """
    if len(sys.argv) < 2:
        return cmd_help()

    command = sys.argv[1].lower()

    # Handle auth command with optional --from-browser / --paste flags
    if command == "auth":
        from_browser = None
        args = sys.argv[2:]
        paste = "--paste" in args
        if "--from-browser" in args:
            idx = args.index("--from-browser")
            if idx + 1 < len(args):
                from_browser = args[idx + 1]
            else:
                print("Error: --from-browser requires a browser name (chrome, firefox, auto, etc.)")
                return 1
        return cmd_auth(from_browser=from_browser, paste=paste)

    commands = {
        "auth-status": cmd_auth_status,
        "auth-clear": cmd_auth_clear,
        "config": cmd_config,
        "serve": cmd_serve,
        "serve-http": cmd_serve_http,
        "help": cmd_help,
        "--help": cmd_help,
        "-h": cmd_help,
    }

    if command in commands:
        return commands[command]()
    else:
        print(f"Unknown command: {command}")
        print("Run 'tp-mcp help' for usage.")
        return 1
