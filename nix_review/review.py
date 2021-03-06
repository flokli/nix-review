import sys
import tempfile
import subprocess
import xml.etree.ElementTree as ET
import multiprocessing
import json
import urllib.request
import urllib.parse
import io
from collections import defaultdict
import shlex
from typing import List, Dict, Tuple, Any, DefaultDict, Set, Optional

from .utils import sh


class GithubClient():
    def __init__(self, api_token):
        self.api_token = api_token

    def get(self, path: str) -> Any:
        url = urllib.parse.urljoin("https://api.github.com/", path)
        req = urllib.request.Request(url)
        if self.api_token:
            req.add_header("Authorization", f"token {self.api_token}")
        return json.loads(urllib.request.urlopen(req).read())


class Review():
    def __init__(self,
                 worktree_dir: str,
                 build_args: str,
                 api_token: str = None) -> None:
        self.worktree_dir = worktree_dir
        self.build_args = build_args
        self.github_client = GithubClient(api_token)

    def git_merge(self, commit: str) -> None:
        sh(["git", "merge", "--no-commit", commit], cwd=self.worktree_dir)

    def build_commit(self, base_commit: str,
                     reviewed_commit: str) -> List[str]:
        """
        Review a local git commit
        """
        git_worktree(self.worktree_dir, base_commit)
        base_packages = list_packages(self.worktree_dir)

        self.git_merge(reviewed_commit)

        merged_packages = list_packages(self.worktree_dir, check_meta=True)

        attrs = differences(base_packages, merged_packages)
        return build_in_path(self.worktree_dir, attrs, self.build_args)

    def build_pr(self, pr: Dict[str, Any]) -> List[str]:
        packages_per_system = self.get_borg_eval_gist(pr)
        (base_rev, pr_rev) = fetch_refs(pr["base"]["ref"],
                                        f"pull/{pr['number']}/head")
        if packages_per_system is None:
            return self.build_commit(base_rev, pr_rev)
        else:
            git_worktree(self.worktree_dir, base_rev)
            self.git_merge(pr_rev)
            system = subprocess.check_output(
                ["nix", "eval", "--raw", "nixpkgs.system"]).decode("utf-8")
            packages = packages_per_system[system]
            return build_in_path(self.worktree_dir, packages, self.build_args)

    def review_commit(self, branch: str, reviewed_commit: str) -> None:
        branch_rev = fetch_refs(branch)[0]
        attrs = self.build_commit(branch_rev, reviewed_commit)
        if attrs:
            nix_shell(attrs)

    def review_pr(self, pr_number: int) -> None:
        """
        Review a pull request from the nixpkgs github repository
        """
        pr = self.github_client.get(f"repos/NixOS/nixpkgs/pulls/{pr_number}")
        attrs = self.build_pr(pr)
        if attrs:
            nix_shell(attrs)

    def get_borg_eval_gist(self, pr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        packages_per_system: DefaultDict[str, list] = defaultdict(list)
        statuses = self.github_client.get(pr["statuses_url"])
        for status in statuses:
            url = status.get("target_url", "")
            if status["description"] == "^.^!" and \
               status["creator"]["login"] == "GrahamcOfBorg" and \
               url != "":
                url = urllib.parse.urlparse(url)
                raw_gist_url = f"https://gist.githubusercontent.com/GrahamcOfBorg{url.path}/raw/"
                for line in urllib.request.urlopen(raw_gist_url):
                    if line == b"":
                        break
                    system, attribute = line.decode("utf-8").split()
                    packages_per_system[system].append(attribute)
                return packages_per_system
        return None


def nix_shell(attrs: List[str]) -> None:
    cmd = ["nix-shell"]
    for a in attrs:
        cmd.append(f"-p")
        cmd.append(a)
    sh(cmd)


def git_worktree(worktree_dir: str, commit: str) -> None:
    sh(["git", "worktree", "add", worktree_dir, commit])


def filter_broken_attrs(attrs: Set[str]) -> List[str]:
    expression = "(with import <nixpkgs> {}; {\n"
    for attr in attrs:
        expression += '\t"%s" = (builtins.tryEval "${%s}").success;\n' % (attr,
                                                                          attr)
    expression += "})"
    cmd = ["nix", "eval", "--json", expression]
    evaluates = json.loads(subprocess.check_output(cmd))
    return list(filter(lambda attr: evaluates[attr], attrs))


def build_in_path(path: str, attrs: Set[str], args: str) -> List[str]:
    if not attrs:
        print("Nothing changed")
        return []

    result_dir = tempfile.mkdtemp(prefix="nox-review-")
    working_attrs = filter_broken_attrs(attrs)
    if not working_attrs:
        print(
            f"the following packages are marked as broken and where skipped: {' '.join(attrs)}"
        )
        return working_attrs
    print("Building in {}: {}".format(result_dir, " ".join(working_attrs)))
    command = [
        "nix-shell",
        "--no-out-link",
        "--keep-going",
        "--max-jobs",
        str(multiprocessing.cpu_count()),
        # only matters for single-user nix and trusted users
        "--option",
        "build-use-sandbox",
        "true",
        "--run",
        "true",
    ] + shlex.split(args)
    for a in working_attrs:
        command.append("-p")
        command.append(a)

    try:
        sh(command, cwd=result_dir)
        return working_attrs
    except subprocess.CalledProcessError:
        msg = f"The invocation of '{' '.join(command)}' failed\n\n"
        msg += "Your NIX_PATH still points to the merged pull requests, so you can make attempts to fix it and rerun the command above"
        print(msg, file=sys.stderr)
        # XXX personal nit to use bash here,
        # since my zsh overrides NIX_PATH.
        sh(["bash"], cwd=result_dir)
        raise


PackageSet = Set[Tuple[str, str]]


def list_packages(path: str, check_meta=False) -> PackageSet:
    cmd = [
        "nix-env", "-f", path, "-qaP", "--xml", "--out-path", "--show-trace"
    ]
    if check_meta:
        cmd.append("--meta")
    output = subprocess.check_output(cmd)
    context = ET.iterparse(
        io.StringIO(output.decode("utf-8")), events=("start", ))
    packages = set()
    for (event, elem) in context:
        if elem.tag == "item":
            attrib = elem.attrib["attrPath"]
        elif elem.tag == "output":
            assert attrib is not None
            path = elem.attrib["path"]
            packages.add((attrib, path))
    return packages


def fetch_refs(*refs: str) -> List[str]:
    cmd = ["git", "fetch", "--force", "https://github.com/NixOS/nixpkgs"]
    for i, ref in enumerate(refs):
        cmd.append(f"{ref}:refs/nix-review/{i}")
    sh(cmd)
    shas = []
    for i, ref in enumerate(refs):
        o = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"refs/nix-review/{i}"])
        shas.append(o.strip().decode("utf-8"))
    return shas


def differences(old: PackageSet, new: PackageSet) -> Set[str]:
    raw = new - old
    return {l[0] for l in raw}
