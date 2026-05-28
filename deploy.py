import os
import json
import subprocess
import requests
import base64
import time
from azure.identity import ClientSecretCredential

# -------------------------------
# ✅ CONFIG
# -------------------------------
env = os.getenv("ENVIRONMENT", "dev")

with open(f".deploy/{env}.json") as f:
    config = json.load(f)

workspace_id = config["workspace_id"]

# -------------------------------
# ✅ AUTH
# -------------------------------
def get_env(name):
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} missing")
    return value


credential = ClientSecretCredential(
    tenant_id=get_env("TENANT_ID"),
    client_id=get_env("CLIENT_ID"),
    client_secret=get_env("CLIENT_SECRET")
)


def get_token():
    return credential.get_token(
        "https://api.fabric.microsoft.com/.default"
    ).token


# -------------------------------
# ✅ CHANGE DETECTION (IMPROVED ✅)
# -------------------------------
def get_changed_files():
    try:
        result = subprocess.check_output(
            "git diff --name-only HEAD~1 HEAD", shell=True
        ).decode("utf-8")
        return [f.strip() for f in result.split("\n") if f.strip()]
    except:
        return []


def load_changes():
    if env == "dev":
        return get_changed_files()

    if os.path.exists("changed_files.json"):
        with open("changed_files.json") as f:
            return json.load(f)

    return []


changed_files = load_changes()
# ✅ FILTER NON-FABRIC PATHS
changed_files = [
    f for f in changed_files
    if not f.startswith((".venv", ".deploy", ".vscode"))
]
# Save for promotion
if env == "dev":
    with open("changed_files.json", "w") as f:
        json.dump(changed_files, f)

if not changed_files:
    print("⚠️ No changes → skipping")
    exit(0)

print("📂 Changed:", changed_files)

# -------------------------------
# ✅ ARTIFACT ROOT
# -------------------------------
def get_artifact_root(path):
    parts = path.split(os.sep)

    for i, part in enumerate(parts):
        if part.endswith((
    ".Notebook",
    ".DataPipeline",
    ".Report",
    ".SemanticModel"
)) and not part.startswith("."):

            return os.sep.join(parts[:i + 1])

    return None


artifact_roots = set()

for f in changed_files:
    root = get_artifact_root(f)
    if root:
        artifact_roots.add(root)

artifact_roots = list(artifact_roots)

if not artifact_roots:
    print("⚠️ No artifacts to deploy")
    exit(0)

print("📦 Final deployment:", artifact_roots)

# -------------------------------
# ✅ TYPE
# -------------------------------
def get_type(path):
    if path.endswith(".Notebook"):
        return "Notebook"
    if path.endswith(".DataPipeline"):
        return "DataPipeline"
    if path.endswith(".Report"):
        return "Report"
    if path.endswith(".SemanticModel"):
        return "SemanticModel"
    return None


# -------------------------------
# ✅ BUILD DEF
# -------------------------------
def load_definition(path):
    parts = []

    for root, _, files in os.walk(path):
        for f in files:
            if f.startswith(".") and f != ".platform":
                continue

            full_path = os.path.join(root, f)

            with open(full_path, "rb") as file:
                content = base64.b64encode(file.read()).decode("utf-8")

            relative_path = os.path.relpath(full_path, path)

            parts.append({
                "path": relative_path.replace("\\", "/"),
                "payload": content,
                "payloadType": "InlineBase64"
            })

    return {"parts": parts} if parts else None


# -------------------------------
# ✅ GET EXISTING (FIXED ✅ returns FULL ITEM)
# -------------------------------
def get_existing_item(name, headers):
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items"

    res = requests.get(url, headers=headers)

    if res.status_code == 200:
        for item in res.json().get("value", []):
            if item["displayName"].strip().lower() == name.strip().lower():
                return item

    return None


# -------------------------------
# ✅ ASYNC HANDLER (ENHANCED ✅)
# -------------------------------
def wait_for_operation(res, headers, name):

    if res.status_code == 202:
        print("⏳ Waiting for async operation...")

        location = res.headers.get("Location")

        if not location:
            return res

        for _ in range(15):
            time.sleep(2)
            check = requests.get(location, headers=headers)

            if check.status_code == 200:
                print(f"✅ Completed: {name}")
                return check

        print(f"⚠️ Timeout: {name}")
        return res

    # ✅ HANDLE 409 retry (NEW FIX)
    if res.status_code == 409:
        try:
            err = res.json()
            if err.get("errorCode") == "ItemDisplayNameNotAvailableYet":
                print(f"🔁 Retry needed for {name}")
                time.sleep(30)
                return "RETRY"
        except:
            pass

    return res


# -------------------------------
# ✅ DEPLOY (FINAL FIXED LOGIC ✅)
# -------------------------------
def deploy(files):

    token = get_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    base_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items"

    for f in files:

        if not os.path.isdir(f):
            continue

        item_type = get_type(f)
        if not item_type:
            continue

        definition = load_definition(f)
        if not definition:
            continue

        name = os.path.basename(f).replace(f".{item_type}", "")

        print(f"\n📦 Processing: {name} ({item_type})")

        existing = get_existing_item(name, headers)

        # ---------------------------
        # ✅ UPDATE (BEST PRACTICE)
        # ---------------------------
        if existing:
            print(f"✅ Found existing in {env.upper()}: {name}")

            item_id = existing["id"]
            update_url = f"{base_url}/{item_id}"

            # ✅ PIPELINE still recreate
            if item_type == "DataPipeline":
                print(f"⚠️ Recreating DataPipeline: {name}")

                delete_url = update_url
                del_res = requests.delete(delete_url, headers=headers)

                if del_res.status_code not in [200, 202, 204]:
                    print(f"❌ DELETE FAILED: {del_res.text}")
                    continue

                print(f"✅ Deleted: {name}")
                time.sleep(5)

                existing = None

        # ---------------------------
        # ✅ RECREATE (FIXED ✅)
        # ---------------------------
        if existing:
            print(f"✅ Found existing in {env.upper()}: {name}")

            item_id = existing["id"]
            delete_url = f"{base_url}/{item_id}"

            print(f"⚠️ Recreating {item_type}: {name}")

            for attempt in range(5):
                del_res = requests.delete(delete_url, headers=headers)

                if del_res.status_code in [200, 202, 204]:
                    print(f"✅ Deleted: {name}")
                    break
                else:
                    print(f"❌ Delete failed: {del_res.text}")
                    time.sleep(5)

            # ✅ VERY IMPORTANT (prevents 409)
            time.sleep(30)

            existing = None

        # ---------------------------
        # ✅ CREATE (SAFE)
        # ---------------------------
        print(f"🆕 Creating {name}")

        for attempt in range(5):

            res = requests.post(base_url, headers=headers, json={
                "displayName": name,
                "type": item_type,
                "definition": definition
            })

            result = wait_for_operation(res, headers, name)

            if result == "RETRY":
                continue

            if res.status_code in [200, 201, 202]:
                print(f"✅ Created: {name}")
                break
            else:
                print(f"❌ Create failed: {res.text}")
                time.sleep(10)

        time.sleep(1)


# -------------------------------
# ✅ EXECUTE
# -------------------------------
print(f"\n🚀 Deploying to {env.upper()}")
deploy(artifact_roots)
print("\n✅ DONE")
