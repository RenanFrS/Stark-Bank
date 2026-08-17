"""Generate the secp256k1 key pair used to sign API requests.

Run this once, locally. Upload `keys/public-key.pem` when creating the Project
in the Stark Bank sandbox console, and keep `keys/private-key.pem` out of git.

    python scripts/generate_keys.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import starkbank  # noqa: E402

OUTPUT_DIR = Path("keys")


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    private_key, public_key = starkbank.key.create(str(OUTPUT_DIR))

    print(f"Keys written to {OUTPUT_DIR.resolve()}")
    print()
    print("Next steps:")
    print("  1. Log into https://web.sandbox.starkbank.com")
    print("  2. Menu > Integrations > New Project")
    print(f"  3. Upload {OUTPUT_DIR / 'public-key.pem'}")
    print("  4. Copy the Project ID into STARKBANK_PROJECT_ID in your .env")
    print("  5. Point STARKBANK_PRIVATE_KEY_PATH at keys/private-key.pem")
    print()
    print("Never commit the private key.")

    _ = private_key, public_key


if __name__ == "__main__":
    main()
