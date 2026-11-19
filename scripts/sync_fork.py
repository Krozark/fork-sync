#!/usr/bin/env python3
import argparse
import configparser
import fcntl
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import yaml


def normalize_repo_key(url: str) -> str:
    """Normalise une URL Git pour comparer facilement deux dépôts."""
    cleaned = url.strip()
    if cleaned.startswith("git+"):
        cleaned = cleaned[4:]
    parsed = urlparse(cleaned)
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{parsed.netloc.lower()}{path.lower()}"


def suggest_repo_name(url: str) -> str:
    """Extrait un nom de dépôt propre depuis une URL Git."""
    parsed = urlparse(url.strip())
    name = Path(parsed.path.rstrip("/")).name
    if name.endswith(".git"):
        name = name[:-4]
    return name or "unknown-submodule"


def normalize_upstream_url(url: str) -> str:
    """Normalise l'URL upstream pour forks.yml."""
    cleaned = url.strip()
    if cleaned.startswith("git+"):
        cleaned = cleaned[4:]
    if cleaned.startswith(("http://", "https://")) and not cleaned.endswith(".git"):
        cleaned = f"{cleaned.rstrip('/')}.git"
    return cleaned


def sanitize_output(text: str, token: str) -> str:
    """Masque le token s'il apparaît dans un message d'erreur ou une commande."""
    if token:
        return text.replace(token, "***")
    return text


def github_request(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, dict | None]:
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"raw": sanitize_output(body, token)}


def ensure_repo_exists(owner: str, repo_name: str, upstream_url: str, token: str) -> None:
    status, _ = github_request("GET", f"/repos/{owner}/{repo_name}", token)
    if status == 200:
        print(f"[ok] Dépôt {owner}/{repo_name} déjà existant.")
        return

    if status != 404:
        raise RuntimeError(f"Échec vérification dépôt {owner}/{repo_name} (HTTP {status})")

    print(f"[info] Dépôt {owner}/{repo_name} introuvable (404), création en cours...")
    create_status, err = github_request(
        "POST",
        "/user/repos",
        token,
        payload={
            "name": repo_name,
            "description": f"Mirror of {upstream_url}",
            "private": False,
        },
    )
    if create_status not in (200, 201):
        raise RuntimeError(f"Échec création dépôt {owner}/{repo_name} (HTTP {create_status}): {err}")
    print(f"[ok] Dépôt {owner}/{repo_name} créé.")


def run_git(
    args: list[str],
    cwd: Path | None = None,
    capture: bool = False,
    token: str = "",
) -> str:
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=capture,
        )
        return res.stdout if capture else ""
    except subprocess.CalledProcessError as exc:
        safe_cmd = [sanitize_output(part, token) for part in exc.cmd]
        safe_stderr = sanitize_output(exc.stderr or "", token)
        msg = f"Commande Git échouée ({exc.returncode}): {' '.join(safe_cmd)}"
        if safe_stderr.strip():
            msg += f"\n{safe_stderr.strip()}"
        raise RuntimeError(msg) from None


def write_missing_submodules_yaml(output_path: Path, new_items: list[dict]) -> None:
    """Ajoute les sous-modules manquants sans doublon dans un fichier YAML global."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path.with_suffix(output_path.suffix + ".lock")

    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            existing_data = {}
            if output_path.exists():
                existing_data = yaml.safe_load(output_path.read_text(encoding="utf-8")) or {}

            existing_forks = existing_data.get("forks") or []
            known_keys = {
                normalize_repo_key(entry["upstream"])
                for entry in existing_forks
                if entry.get("upstream")
            }

            for item in new_items:
                key = normalize_repo_key(item["upstream"])
                if key in known_keys:
                    continue
                known_keys.add(key)
                existing_forks.append({
                    "name": item["name"],
                    "upstream": item["upstream"],
                    "source_parent": item["from_repo"],
                })

            payload = {"forks": existing_forks}
            output_path.write_text(
                yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def check_submodules(
    bare_repo: Path,
    parent_name: str,
    configured_upstreams: set[str],
    missing_output_path: Path | None = None,
    strict: bool = False,
) -> None:
    """Vérifie si les sous-modules de .gitmodules sont dans forks.yml et les exporte dans un YAML global."""
    branches = run_git(
        ["for-each-ref", "--format=%(refname)", "refs/heads"],
        cwd=bare_repo,
        capture=True,
    ).splitlines()
    refs_to_try = ["HEAD", *branches]

    gitmodules_content = None
    for ref in refs_to_try:
        try:
            gitmodules_content = run_git(["show", f"{ref}:.gitmodules"], cwd=bare_repo, capture=True)
            break
        except RuntimeError:
            continue

    if not gitmodules_content:
        return

    parser = configparser.ConfigParser()
    parser.read_string(gitmodules_content)

    missing = []
    for section in parser.sections():
        sub_url = parser[section].get("url", "").strip()
        if not sub_url:
            continue
        if normalize_repo_key(sub_url) not in configured_upstreams:
            missing.append({
                "name": suggest_repo_name(sub_url),
                "upstream": normalize_upstream_url(sub_url),
                "from_repo": parent_name,
            })

    if not missing:
        print("[ok] Tous les sous-modules de .gitmodules sont référencés dans forks.yml.")
        return

    for item in missing:
        print(f"[warn] Sous-module non trouvé dans forks.yml : {item['name']} -> {item['upstream']}")

    if missing_output_path is not None:
        write_missing_submodules_yaml(missing_output_path, missing)
        print(f"[info] Sous-modules manquants ajoutés à {missing_output_path}")

    if strict:
        raise RuntimeError("Des sous-modules de .gitmodules ne sont pas configurés dans forks.yml.")


def branch_touches_workflows(bare_repo: Path, ref: str) -> bool:
    """Retourne True si le commit de la branche modifie un fichier dans .github/workflows/."""
    try:
        out = run_git(
            ["diff-tree", "-r", "--name-only", f"{ref}^1", ref, "--", ".github/workflows"],
            cwd=bare_repo,
            capture=True,
        )
        return bool(out.strip())
    except RuntimeError:
        out = run_git(
            ["diff-tree", "--root", "-r", "--name-only", ref, "--", ".github/workflows"],
            cwd=bare_repo,
            capture=True,
        )
        return bool(out.strip())


def push_refspec(
    bare_repo: Path,
    refspec: str,
    auth_fork_url: str,
    fork_url: str,
    token: str,
    skip_workflow_branches: bool,
) -> None:
    if refspec == "refs/heads/*:refs/heads/*" and skip_workflow_branches:
        heads = run_git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads"],
            cwd=bare_repo,
            capture=True,
        ).splitlines()

        safe_heads = [h for h in heads if not branch_touches_workflows(bare_repo, f"refs/heads/{h}")]
        skipped = sorted(set(heads) - set(safe_heads))

        for s in skipped:
            print(f"[warn] Branche '{s}' ignorée (touche .github/workflows/ et --skip-workflow-branches actif).")

        for h in safe_heads:
            print(f"[info] Push refs/heads/{h}:refs/heads/{h} vers {fork_url}...")
            run_git(
                ["push", auth_fork_url, f"refs/heads/{h}:refs/heads/{h}", "--force"],
                cwd=bare_repo,
                token=token,
            )
        return

    print(f"[info] Push {refspec} vers {fork_url}...")
    run_git(["push", auth_fork_url, refspec, "--force"], cwd=bare_repo, token=token)


def sync_entry(
    config_path: Path,
    entry_name: str,
    token: str,
    strict_submodules: bool,
    skip_workflow_branches: bool,
    missing_submodules_file: Path | None,
) -> None:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    owner = config.get("owner", "Krozark")
    default_refs = config.get("default_refs") or [
        "refs/heads/*:refs/heads/*",
        "refs/tags/*:refs/tags/*",
    ]
    forks = config.get("forks") or []

    configured_upstreams = {
        normalize_repo_key(item["upstream"])
        for item in forks
        if item.get("upstream")
    }

    target = next((item for item in forks if item.get("name") == entry_name), None)
    if not target:
        raise KeyError(f"Entrée '{entry_name}' introuvable dans {config_path}")

    upstream_url = target["upstream"]
    fork_url = target.get("fork") or f"https://github.com/{owner}/{entry_name}.git"
    refs = target.get("refs") or default_refs

    ensure_repo_exists(owner, entry_name, upstream_url, token)

    work_dir = Path(f"repo-{entry_name}.git")
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)

    auth_fork_url = fork_url.replace("https://", f"https://x-access-token:{token}@", 1)

    try:
        print(f"[info] Clonage bare de {upstream_url}...")
        run_git(["clone", "--mirror", upstream_url, str(work_dir)], token=token)

        check_submodules(
            bare_repo=work_dir,
            parent_name=entry_name,
            configured_upstreams=configured_upstreams,
            missing_output_path=missing_submodules_file,
            strict=strict_submodules,
        )

        for refspec in refs:
            push_refspec(
                bare_repo=work_dir,
                refspec=refspec,
                auth_fork_url=auth_fork_url,
                fork_url=fork_url,
                token=token,
                skip_workflow_branches=skip_workflow_branches,
            )
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)

    print(f"[ok] Synchronisation terminée pour {entry_name}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronise un dépôt amont vers un fork GitHub.")
    parser.add_argument("--config", default="forks.yml", help="Chemin vers forks.yml")
    parser.add_argument("--name", required=True, help="Nom de l'entrée dans forks.yml")
    parser.add_argument(
        "--strict-submodules",
        action="store_true",
        help="Échoue si un sous-module de .gitmodules est absent de forks.yml",
    )
    parser.add_argument(
        "--skip-workflow-branches",
        action="store_true",
        help="Ignore les branches dont le commit modifie .github/workflows/",
    )
    parser.add_argument(
        "--missing-submodules-file",
        default="missing_submodules.yml",
        help="Fichier YAML global où lister les sous-modules manquants",
    )
    args = parser.parse_args()

    token = os.environ.get("FORK_PUSH_TOKEN", "")
    if not token:
        print("[error] Variable d'environnement FORK_PUSH_TOKEN manquante.", file=sys.stderr)
        return 1

    missing_file = Path(args.missing_submodules_file) if args.missing_submodules_file else None

    try:
        sync_entry(
            config_path=Path(args.config),
            entry_name=args.name,
            token=token,
            strict_submodules=args.strict_submodules,
            skip_workflow_branches=args.skip_workflow_branches,
            missing_submodules_file=missing_file,
        )
    except Exception as exc:
        print(f"[error] {sanitize_output(str(exc), token)}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
