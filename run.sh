#export FORK_PUSH_TOKEN="ton_token_github"
python3 -c 'import yaml; data = yaml.safe_load(open("forks.yml")); print("\n".join(x["name"] for x in data.get("forks", [])))' \
  | xargs -I{} -P 4 bash -c 'echo "=== {} ==="; python3 scripts/sync_fork.py --config forks.yml --name "{}" || echo "[fail] {}"'
