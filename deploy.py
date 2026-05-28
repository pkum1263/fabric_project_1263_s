import os
import json
import subprocess
import requests
import base64
import time
from azure.identity import ClientSecretCredential
from concurrent.futures import ThreadPoolExecutor, as_completed

env = os.getenv("ENVIRONMENT", "dev")
workspace_id = os.getenv("WORKSPACE_ID")

if not workspace_id:
    raise ValueError("WORKSPACE_ID environment variable is not set")

DEP_ORDER = ["SemanticModel", "Notebook", "Report", "DataPipeline"]


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

changed_files = [
    f for f in changed_files
    if not f.startswith((".venv", ".vscode"))
]

if env == "dev":
    with open("changed_files.json", "w") as f:
        json.dump(changed_files, f)

if not changed_files:
    print("No changes to deploy")
    exit(0)

print("Changed:", changed_files)


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
    print("No artifacts to deploy")
    exit(0)

print("Final deployment:", artifact_roots)


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

    parts.sort(key=lambda p: (p["path"] != ".platform", p["path"]))

    return {"parts": parts} if parts else None


def get_existing_item(name, headers):
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items"

    res = requests.get(url, headers=headers)

    if res.status_code == 200:
        for item in res.json().get("value", []):
            if item["displayName"].strip().lower() == name.strip().lower():
                return item

    return None


def wait_for_operation(res, headers, name):

    if res.status_code == 202:
        print("Waiting for async operation...")

        location = res.headers.get("Location")

        if not location:
            return res

        for _ in range(15):
            time.sleep(2)
            check = requests.get(location, headers=headers)

            if check.status_code == 200:
                print(f"Completed: {name}")
                return check

        print(f"Timeout: {name}")
        return res

    if res.status_code == 409:
        try:
            err = res.json()
            if err.get("errorCode") == "ItemDisplayNameNotAvailableYet":
                print(f"Retry needed for {name}")
                time.sleep(30)
                return "RETRY"
        except:
            pass

    return res


def get_existing_definition(item_id, headers):
    url = (
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}"
        f"/items/{item_id}/getDefinition"
    )
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        data = res.json()
        return data.get("definition")
    return None


def restore_item(name, item_type, definition, headers):
    base_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items"
    print(f"Rolling back: {name}")

    for attempt in range(5):
        res = requests.post(base_url, headers=headers, json={
            "displayName": name,
            "type": item_type,
            "definition": definition
        })

        result = wait_for_operation(res, headers, f"{name} (rollback)")

        if result == "RETRY":
            continue

        if res.status_code in [200, 201, 202]:
            print(f"Rollback successful: {name}")
            return True

        print(f"Rollback attempt {attempt + 1} failed: {res.text}")
        time.sleep(10)

    print(f"Rollback FAILED: {name} — manual intervention required")
    return False


def deploy_item(f, token):
    if not os.path.isdir(f):
        return True

    item_type = get_type(f)
    if not item_type:
        return True

    definition = load_definition(f)
    if not definition:
        return True

    name = os.path.basename(f).replace(f".{item_type}", "")

    print(f"\nProcessing: {name} ({item_type})")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    base_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items"

    existing = get_existing_item(name, headers)
    old_definition = None

    if existing:
        print(f"Found existing in {env.upper()}: {name}")
        old_definition = get_existing_definition(existing["id"], headers)

        item_id = existing["id"]
        delete_url = f"{base_url}/{item_id}"

        print(f"Recreating {item_type}: {name}")

        for attempt in range(5):
            del_res = requests.delete(delete_url, headers=headers)

            if del_res.status_code in [200, 202, 204]:
                print(f"Deleted: {name}")
                break
            else:
                print(f"Delete failed (attempt {attempt + 1}/5): {del_res.text}")
                time.sleep(5)

        time.sleep(30)

    print(f"Creating {name}")

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
            print(f"Created: {name}")
            time.sleep(1)
            return True

        print(f"Create failed (attempt {attempt + 1}/5): {res.text}")
        time.sleep(10)

    if old_definition:
        print(f"Create failed for {name} — attempting rollback")
        restore_item(name, item_type, old_definition, headers)

    return False


def deploy(files):
    token = get_token()

    grouped = {t: [] for t in DEP_ORDER}
    for f in files:
        t = get_type(f)
        if t in grouped:
            grouped[t].append(f)

    all_success = True

    for item_type in DEP_ORDER:
        batch = grouped[item_type]
        if not batch:
            continue

        print(f"\n=== Deploying {len(batch)} {item_type}(s) ===")

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(deploy_item, f, token): f for f in batch}
            for future in as_completed(futures):
                f = futures[future]
                if not future.result():
                    print(f"FAILED: {f}")
                    all_success = False

    if not all_success:
        print("\nSome items failed to deploy")
        exit(1)


print(f"\nDeploying to {env.upper()}")
deploy(artifact_roots)
print("\nDONE")
