#!/usr/bin/env python3
"""
Full initialization of all Alex infrastructure from scratch.
Creates the state bucket, then applies terraform in order, packages lambdas,
runs database migrations/seeds, and deploys the frontend.

Usage:
    cd terraform
    uv run initialize.py

Prerequisites:
    - AWS CLI configured (aws configure)
    - Docker Desktop running (for Lambda packaging)
    - terraform.tfvars configured in each directory (2-8)
    - Node.js and npm installed (for frontend)
    - frontend/.env.local configured (Clerk keys)
"""

import subprocess
import sys
import os
from pathlib import Path


TERRAFORM_DIR = Path(__file__).parent
PROJECT_ROOT = TERRAFORM_DIR.parent
BACKEND_DIR = PROJECT_ROOT / "backend"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

# Apply order: sequential because later dirs depend on earlier ones.
# Dir 5 (database) is included here since this is a fresh setup.
# After dir 5, we run migrations and seed data before continuing.
INFRA_DIRS = ["2_sagemaker", "3_ingestion", "4_researcher", "5_database", "6_agents", "7_frontend", "8_enterprise"]


def run(cmd, cwd=None, capture=False):
    """Run a command. Returns (success, stdout) if capture=True, else (success, None)."""
    print(f"  > {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    if capture:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=isinstance(cmd, str))
        return result.returncode == 0, result.stdout.strip()
    else:
        result = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str))
        return result.returncode == 0, None


def check_prerequisites():
    """Verify all required tools and configs are available."""
    print("🔍 Checking prerequisites...")

    # Check tools
    tools = ["terraform", "aws", "docker", "npm", "uv"]
    for tool in tools:
        ok, _ = run([tool, "--version"], capture=True)
        if ok:
            print(f"  ✅ {tool}")
        else:
            print(f"  ❌ {tool} not found")
            return False

    # Check Docker is running
    ok, _ = run(["docker", "info"], capture=True)
    if not ok:
        print("  ❌ Docker is not running. Start Docker Desktop first.")
        return False
    print("  ✅ Docker is running")

    # Check AWS credentials
    ok, _ = run(["aws", "sts", "get-caller-identity"], capture=True)
    if not ok:
        print("  ❌ AWS credentials not configured. Run 'aws configure'")
        return False
    print("  ✅ AWS credentials configured")

    # Check terraform.tfvars exist
    missing_tfvars = []
    for d in INFRA_DIRS:
        tfvars = TERRAFORM_DIR / d / "terraform.tfvars"
        if not tfvars.exists():
            missing_tfvars.append(d)
    if missing_tfvars:
        print(f"\n  ❌ Missing terraform.tfvars in: {', '.join(missing_tfvars)}")
        print("     Copy terraform.tfvars.example to terraform.tfvars and configure each one.")
        return False
    print("  ✅ All terraform.tfvars present")

    # Check frontend env
    env_local = FRONTEND_DIR / ".env.local"
    if not env_local.exists():
        print("  ❌ frontend/.env.local not found. Configure Clerk keys first.")
        return False
    print("  ✅ frontend/.env.local exists")

    return True


def terraform_apply(directory):
    """Initialize and apply terraform in a directory."""
    tf_dir = TERRAFORM_DIR / directory
    print(f"\n  Initializing {directory}...")
    ok, _ = run(["terraform", "init"], cwd=tf_dir)
    if not ok:
        return False

    print(f"  Applying {directory}...")
    ok, _ = run(["terraform", "apply", "-auto-approve"], cwd=tf_dir)
    return ok


def main():
    print("=" * 60)
    print("ALEX INFRASTRUCTURE INITIALIZATION")
    print("=" * 60)
    print()

    if not check_prerequisites():
        print("\n❌ Prerequisites not met. Fix the issues above and retry.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("Starting infrastructure deployment...")
    print("=" * 60)

    # ── Step 1: Package Lambda agents first ──
    # Lambda packages are infrastructure-agnostic (just local Python code).
    # Package them now so zips exist before terraform tries to upload them to S3.
    print(f"\n{'─' * 40}")
    print("📦 Step 1: Package Lambda agents")
    print(f"{'─' * 40}")
    ok, _ = run(["uv", "run", "package_docker.py"], cwd=BACKEND_DIR)
    if not ok:
        print("❌ Agent packaging failed")
        sys.exit(1)
    print("✅ Agents packaged")

    print("  Packaging API Lambda...")
    ok, _ = run(["uv", "run", "package_docker.py"], cwd=BACKEND_DIR / "api")
    if not ok:
        print("❌ API Lambda packaging failed")
        sys.exit(1)
    print("✅ API Lambda packaged")

    print("  Packaging Ingest Lambda...")
    ok, _ = run(["uv", "run", "package.py"], cwd=BACKEND_DIR / "ingest")
    if not ok:
        print("❌ Ingest Lambda packaging failed")
        sys.exit(1)
    print("✅ Ingest Lambda packaged")

    # ── Step 2: Bootstrap (state bucket) ──
    print(f"\n{'─' * 40}")
    print("📦 Step 2: Bootstrap (S3 state bucket)")
    print(f"{'─' * 40}")
    if not terraform_apply("0_bootstrap"):
        print("❌ Bootstrap failed")
        sys.exit(1)
    print("✅ State bucket created")

    # ── Step 3: Apply terraform directories in sequential order ──
    # Now that Lambda packages exist, terraform won't fail on S3 object uploads.
    for directory in INFRA_DIRS:
        print(f"\n{'─' * 40}")
        print(f"📦 {directory}")
        print(f"{'─' * 40}")

        if not terraform_apply(directory):
            print(f"❌ {directory} failed")
            sys.exit(1)
        print(f"✅ {directory} applied")

        # After database: run migrations and seed data.
        # This must happen before dir 6 (agents) because agents query the database.
        if directory == "5_database":
            print("\n  Running database migrations...")
            db_dir = BACKEND_DIR / "database"
            ok, _ = run(["uv", "run", "run_migrations.py"], cwd=db_dir)
            if not ok:
                print("  ❌ Migrations failed")
                sys.exit(1)
            print("  ✅ Migrations complete")

            print("  Running seed data...")
            ok, _ = run(["uv", "run", "seed_data.py"], cwd=db_dir)
            if not ok:
                print("  ❌ Seed data failed")
                sys.exit(1)
            print("  ✅ Seed data loaded")

    # ── Step 4: Build and deploy frontend ──
    print(f"\n{'─' * 40}")
    print("📦 Building and deploying frontend")
    print(f"{'─' * 40}")

    # Get outputs from 7_frontend
    tf7_dir = TERRAFORM_DIR / "7_frontend"
    ok, api_url = run(["terraform", "output", "-raw", "api_gateway_url"], cwd=tf7_dir, capture=True)
    if not ok:
        print("❌ Could not get API URL from terraform output")
        sys.exit(1)

    ok, s3_bucket = run(["terraform", "output", "-raw", "s3_bucket_name"], cwd=tf7_dir, capture=True)
    if not ok:
        print("❌ Could not get S3 bucket from terraform output")
        sys.exit(1)

    ok, cf_url = run(["terraform", "output", "-raw", "cloudfront_url"], cwd=tf7_dir, capture=True)

    # Write .env.production.local with API URL
    env_prod = FRONTEND_DIR / ".env.production.local"
    env_local = FRONTEND_DIR / ".env.local"
    with open(env_local, "r") as f:
        lines = f.readlines()

    # Update API URL for production build
    new_lines = []
    api_updated = False
    for line in lines:
        if line.startswith("NEXT_PUBLIC_API_URL="):
            new_lines.append(f"NEXT_PUBLIC_API_URL={api_url}\n")
            api_updated = True
        else:
            new_lines.append(line)
    if not api_updated:
        new_lines.append(f"\nNEXT_PUBLIC_API_URL={api_url}\n")

    with open(env_prod, "w") as f:
        f.writelines(new_lines)
    print(f"  ✅ Created .env.production.local with API URL: {api_url}")

    # Install deps + build
    print("  Installing frontend dependencies...")
    ok, _ = run(["npm", "ci"], cwd=FRONTEND_DIR)
    if not ok:
        print("❌ npm ci failed")
        sys.exit(1)

    print("  Building frontend...")
    build_env = os.environ.copy()
    build_env["NODE_ENV"] = "production"
    result = subprocess.run(["npm", "run", "build"], cwd=FRONTEND_DIR, env=build_env)
    if result.returncode != 0:
        print("❌ Frontend build failed")
        sys.exit(1)

    # Sync to S3
    print(f"  Uploading to S3 bucket: {s3_bucket}...")
    out_dir = FRONTEND_DIR / "out"
    ok, _ = run(["aws", "s3", "sync", str(out_dir), f"s3://{s3_bucket}/", "--delete"])
    if not ok:
        print("❌ S3 sync failed")
        sys.exit(1)

    # Invalidate CloudFront
    print("  Invalidating CloudFront cache...")
    # Get distribution ID from CloudFront URL
    ok, cf_domain = run(["terraform", "output", "-raw", "cloudfront_url"], cwd=tf7_dir, capture=True)
    if ok and cf_domain:
        # List distributions and find the matching one
        ok, dist_json = run(
            ["aws", "cloudfront", "list-distributions", "--query",
             "DistributionList.Items[].{Id:Id,Domain:DomainName}", "--output", "json"],
            capture=True
        )
        if ok:
            import json
            try:
                dists = json.loads(dist_json)
                domain = cf_domain.replace("https://", "")
                for d in dists:
                    if d["Domain"] == domain:
                        run(["aws", "cloudfront", "create-invalidation",
                             "--distribution-id", d["Id"], "--paths", "/*", "--no-cli-pager"])
                        print(f"  ✅ CloudFront cache invalidated")
                        break
            except (json.JSONDecodeError, KeyError):
                print("  ⚠️  Could not invalidate CloudFront (non-critical)")

    # ── Summary ──
    print(f"\n{'=' * 60}")
    print("✅ INITIALIZATION COMPLETE")
    print(f"{'=' * 60}")
    print(f"\n  CloudFront URL: {cf_url if cf_url else 'check terraform output'}")
    print(f"  API URL:        {api_url}")
    print(f"  S3 Bucket:      {s3_bucket}")
    print()
    print("  Next steps:")
    print("  1. Verify the frontend loads at the CloudFront URL")
    print("  2. Sign in with Clerk and test the application")
    print("  3. Push to GitHub to trigger CI/CD for future changes")


if __name__ == "__main__":
    main()
