#!/usr/bin/env python3
"""
Full teardown of all Alex infrastructure.
Destroys terraform resources in reverse order, then removes the state bucket.

Usage:
    cd terraform
    uv run teardown.py                    # Destroys dirs 2-4, 6-8 (skips database)
    uv run teardown.py --include-database # Also destroys dir 5 (Aurora)
    uv run teardown.py --include-bootstrap # Also destroys the state bucket + DynamoDB
"""

import subprocess
import sys
from pathlib import Path


TERRAFORM_DIR = Path(__file__).parent

# Reverse order: highest number first.
# This respects dependency ordering — higher-numbered dirs may reference
# resources created by lower-numbered dirs (e.g., dir 7 uses dir 6's SQS queue).
# Dir 5 (Aurora) is excluded by default to prevent accidental data loss.
DESTROY_ORDER = ["8_enterprise", "7_frontend", "6_agents", "4_researcher", "3_ingestion", "2_sagemaker"]


def run(cmd, cwd=None):
    """Run a command, showing output in real time."""
    print(f"  > {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    return result.returncode == 0


def terraform_destroy(directory):
    """Run terraform destroy in a directory."""
    tf_dir = TERRAFORM_DIR / directory
    if not tf_dir.exists():
        print(f"  ⚠️  Directory {directory} not found, skipping")
        return True

    tfvars = tf_dir / "terraform.tfvars"
    tfstate = tf_dir / "terraform.tfstate"
    has_state_local = tfstate.exists()

    # Check if terraform is initialized
    if not (tf_dir / ".terraform").exists():
        print(f"  Initializing terraform in {directory}...")
        if not run(["terraform", "init"], cwd=tf_dir):
            print(f"  ❌ Failed to init {directory}")
            return False

    print(f"  Destroying {directory}...")
    success = run(["terraform", "destroy", "-auto-approve"], cwd=tf_dir)
    if success:
        print(f"  ✅ {directory} destroyed")
    else:
        print(f"  ❌ {directory} destroy failed")
    return success


def main():
    include_database = "--include-database" in sys.argv
    include_bootstrap = "--include-bootstrap" in sys.argv

    print("=" * 60)
    print("ALEX INFRASTRUCTURE TEARDOWN")
    print("=" * 60)
    print()
    print("This will destroy the following infrastructure:")
    for d in DESTROY_ORDER:
        print(f"  - {d}")
    if include_database:
        print(f"  - 5_database (Aurora Serverless v2 - ALL DATA WILL BE LOST)")
    else:
        print(f"  - 5_database: SKIPPED (use --include-database to include)")
    if include_bootstrap:
        print(f"  - 0_bootstrap (S3 state bucket + DynamoDB lock table)")
    print()

    confirm = input("Type YES to confirm destruction: ")
    if confirm != "YES":
        print("Aborted.")
        sys.exit(1)

    print()
    results = {}

    # Destroy in reverse order
    for directory in DESTROY_ORDER:
        print(f"\n{'─' * 40}")
        print(f"📦 {directory}")
        print(f"{'─' * 40}")
        results[directory] = terraform_destroy(directory)

    # Database (optional)
    if include_database:
        print(f"\n{'─' * 40}")
        print(f"📦 5_database (Aurora)")
        print(f"{'─' * 40}")
        results["5_database"] = terraform_destroy("5_database")

    # Bootstrap (optional, must be last)
    if include_bootstrap:
        print(f"\n{'─' * 40}")
        print(f"📦 0_bootstrap (state bucket)")
        print(f"{'─' * 40}")
        results["0_bootstrap"] = terraform_destroy("0_bootstrap")

    # Summary
    print(f"\n{'=' * 60}")
    print("TEARDOWN SUMMARY")
    print(f"{'=' * 60}")
    for directory, success in results.items():
        status = "✅ Destroyed" if success else "❌ Failed"
        print(f"  {directory}: {status}")

    failed = [d for d, s in results.items() if not s]
    if failed:
        print(f"\n⚠️  {len(failed)} directories failed. Check output above.")
        sys.exit(1)
    else:
        print(f"\n✅ All infrastructure destroyed successfully.")


if __name__ == "__main__":
    main()
