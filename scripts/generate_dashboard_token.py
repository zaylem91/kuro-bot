"""Generate a cryptographically secure DASHBOARD_TOKEN.

Usage:
    python scripts/generate_dashboard_token.py
    python scripts/generate_dashboard_token.py --length 48

Copy the output into your .env as DASHBOARD_TOKEN=<token>.
Keep it secret — anyone with this token fully controls the bot remotely.
"""
import argparse
import secrets

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Generate DASHBOARD_TOKEN")
    p.add_argument("--length", type=int, default=48,
                   help="token bytes (default 48 -> 64 url-safe chars)")
    args = p.parse_args()
    length = min(max(args.length, 32), 128)
    token = secrets.token_urlsafe(length)
    print(token)
    print()
    print("Add to .env:")
    print(f"DASHBOARD_TOKEN={token}")
