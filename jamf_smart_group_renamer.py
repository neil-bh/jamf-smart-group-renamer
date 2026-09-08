#!/usr/bin/env python3
"""
Jamf Smart Group Renamer

Renames a Jamf Pro smart computer group and repairs everything that referenced it.

Jamf Pro refuses to rename a smart group while another group's criteria reference
it by name. Rather than stripping those criteria and restoring them, which leaves
groups temporarily invalid, this tool clones the group under the new name,
repoints every reference, then removes the original.

Standard library only. No dependencies to install.
"""

import base64
import getpass
import select
import itertools
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

VERSION = "0.3-dev"
TOOL_NAME = "Jamf Smart Group Renamer"

CRITERION_FIELDS = ("name", "priority", "and_or", "search_type", "value",
                    "opening_paren", "closing_paren")

# The criterion type Jamf Pro uses for computer group membership. Any other
# criterion type may hold a value that happens to match a group name, and must
# not be treated as a reference to that group. See issue #3.
GROUP_CRITERION_NAME = "Computer Group"

SITE_FIELDS = ("id", "name")

SCOPED_TYPES = (
    ("Policy", "/JSSResource/policies", "policies", "policy"),
    ("Configuration Profile", "/JSSResource/osxconfigurationprofiles",
     "os_x_configuration_profiles", "os_x_configuration_profile"),
)


class Style:
    enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    AMBER = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[36m"

    @classmethod
    def paint(cls, text, *codes):
        if not cls.enabled:
            return text
        return "".join(codes) + text + cls.RESET

    @classmethod
    def ok(cls, text):
        return cls.paint(text, cls.GREEN)

    @classmethod
    def warn(cls, text):
        return cls.paint(text, cls.AMBER)

    @classmethod
    def bad(cls, text):
        return cls.paint(text, cls.RED)

    @classmethod
    def info(cls, text):
        return cls.paint(text, cls.BLUE)

    @classmethod
    def strong(cls, text):
        return cls.paint(text, cls.BOLD)

    @classmethod
    def faint(cls, text):
        return cls.paint(text, cls.DIM)


TICK = Style.ok("\u2713")
CROSS = Style.bad("\u2717")
DOT = Style.warn("\u2022")


def heading(text):
    print()
    print(Style.strong(text))
    print(Style.faint("-" * max(len(text), 40)))


def guidance_heading(text):
    print()
    print(Style.warn(Style.strong(text)))
    print(Style.warn("-" * max(len(text), 40)))


def banner():
    title = f"{TOOL_NAME} v{VERSION}"
    width = max(len(title) + 4, 52)
    print()
    print(Style.info("=" * width))
    print(Style.info(f"  {Style.strong(title)}"))
    print(Style.info("=" * width))


INPUT_TIMEOUT_SECONDS = 60


class InputTimeout(Exception):
    pass


def report_timeout(expired):
    seconds = int(str(expired))
    if seconds >= 60:
        window = f"{seconds // 60} minute" + ("s" if seconds >= 120 else "")
    else:
        window = f"{seconds} seconds"
    print()
    print(Style.bad(f"  Timed out after {window} without input."))
    print()
    print("  The session has been closed so that an authenticated connection is not")
    print("  left open unattended. Nothing further was changed.")


def ask(message, timeout=INPUT_TIMEOUT_SECONDS):
    """Prompt for input, abandoning the session if left unattended."""
    sys.stdout.write(Style.info(message))
    sys.stdout.flush()
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        raise InputTimeout(timeout)
    return sys.stdin.readline().strip()


class Spinner:
    FRAMES = "|/-\\"

    def __init__(self, message):
        self.message = message
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if sys.stdout.isatty():
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            print(f"  {self.message}...")
        return self

    def _spin(self):
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r  {Style.info(frame)} {self.message}...")
            sys.stdout.flush()
            time.sleep(0.12)

    def __exit__(self, *_):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            sys.stdout.write("\r" + " " * (len(self.message) + 10) + "\r")
            sys.stdout.flush()
        return False


def xml_escape(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def draw_table(headers, rows):
    if not rows:
        return Style.faint("  none")
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    rule = "  +" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [Style.faint(rule),
             "  |" + "|".join(f" {Style.strong(f'{h:<{widths[i]}}')} "
                              for i, h in enumerate(headers)) + "|",
             Style.faint(rule)]
    for row in rows:
        lines.append("  |" + "|".join(f" {str(c):<{widths[i]}} "
                                      for i, c in enumerate(row)) + "|")
    lines.append(Style.faint(rule))
    return "\n".join(lines)


IGNORED_ERROR_TEXT = {"error", "conflict", "status page", "bad request",
                      "forbidden", "not found", "apache tomcat"}


def extract_server_message(response):
    if not response:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", response,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    candidates = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line or line.lower() in IGNORED_ERROR_TEXT:
            continue
        if line.lower().startswith(("type ", "description ", "message ")):
            line = line.split(" ", 1)[1].strip()
        if len(line) > 3:
            candidates.append(line)
    for line in candidates:
        if len(line) > 12:
            return line[:300]
    return candidates[0][:300] if candidates else ""


def extract_dependencies(reason):
    """Pull the object types Jamf names when it refuses to delete a group."""
    match = re.search(r"dependent on this .*? and need to be updated:\s*(.+)",
                      reason or "", re.IGNORECASE)
    if not match:
        return []
    listed = match.group(1).strip().rstrip(".")
    return [part.strip() for part in re.split(r",|\band\b", listed) if part.strip()]


def describe_failure(status, response):
    reasons = {
        400: "the server rejected the request as malformed",
        401: "the session is no longer authenticated",
        403: "the account does not have permission to change this object",
        404: "the object no longer exists",
        409: "the server rejected the update as a conflict",
        500: "the server returned an internal error",
    }
    detail = reasons.get(status, f"the server returned status {status}")
    message = extract_server_message(response)
    if not message:
        return detail
    if len(message) > 40:
        return message
    return f"{detail} - {message}"


class JamfError(Exception):
    pass


class JamfClient:
    def __init__(self, server_url, credentials):
        self.base_url = server_url.rstrip("/")
        self.context = ssl.create_default_context()
        self.credentials = credentials
        self.token = self._new_token()

    def _new_token(self):
        if self.credentials["method"] == "client":
            return self._request_client_token(self.credentials["client_id"],
                                              self.credentials["client_secret"])
        return self._request_token(self.credentials["username"],
                                   self.credentials["password"])

    def _send(self, method, path, body=None, accept="application/json",
              content_type=None, retry=True):
        request = urllib.request.Request(f"{self.base_url}{path}", method=method)
        request.add_header("Accept", accept)
        request.add_header("Authorization", f"Bearer {self.token}")
        if content_type:
            request.add_header("Content-Type", content_type)
        payload = body.encode("utf-8") if body else None
        try:
            with urllib.request.urlopen(request, data=payload, timeout=60,
                                        context=self.context) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            if error.code == 401 and retry:
                # Tokens are short lived, particularly for API clients. Renew once
                # and repeat the request before treating this as a failure.
                self.token = self._new_token()
                return self._send(method, path, body=body, accept=accept,
                                  content_type=content_type, retry=False)
            return error.code, error.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as error:
            raise JamfError(f"Lost contact with the server. {error.reason}")

    def _request_token(self, username, password):
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        request = urllib.request.Request(f"{self.base_url}/api/v1/auth/token", method="POST")
        request.add_header("Authorization", f"Basic {credentials}")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30, context=self.context) as response:
                return json.loads(response.read().decode())["token"]
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise JamfError("Authentication failed. Check the username and password.")
            raise JamfError(f"Authentication failed with status {error.code}.")
        except urllib.error.URLError as error:
            raise JamfError(f"Could not reach {self.base_url}. Check the server address. "
                            f"({error.reason})")

    def _request_client_token(self, client_id, client_secret):
        payload = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }).encode()
        request = urllib.request.Request(f"{self.base_url}/api/oauth/token", method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, data=payload, timeout=30,
                                        context=self.context) as response:
                return json.loads(response.read().decode())["access_token"]
        except urllib.error.HTTPError as error:
            if error.code in (400, 401):
                raise JamfError("Authentication failed. Check the client ID and secret, "
                                "and that the API client is enabled.")
            if error.code == 404:
                raise JamfError("This Jamf Pro version does not support API clients. "
                                "Use a username and password instead.")
            raise JamfError(f"Authentication failed with status {error.code}.")
        except urllib.error.URLError as error:
            raise JamfError(f"Could not reach {self.base_url}. Check the server address. "
                            f"({error.reason})")

    def get_json(self, path):
        status, body = self._send("GET", path)
        if status != 200:
            raise JamfError(f"Could not read {path} (status {status}).")
        return json.loads(body)

    def get_xml(self, path):
        status, body = self._send("GET", path, accept="text/xml")
        if status != 200:
            raise JamfError(f"Could not read {path} (status {status}).")
        return body

    def post_xml(self, path, body):
        return self._send("POST", path, body=body, accept="text/xml", content_type="text/xml")

    def put_xml(self, path, body):
        return self._send("PUT", path, body=body, accept="text/xml", content_type="text/xml")

    def delete(self, path):
        return self._send("DELETE", path, accept="text/xml")

    def logout(self):
        try:
            self._send("POST", "/api/v1/auth/invalidate-token")
        except Exception:
            pass


class SmartGroupRenamer:
    def __init__(self, client):
        self.client = client

    def list_all_groups(self):
        """Every computer group, smart and static.

        Jamf Pro holds smart and static computer group names in one namespace,
        so a new name has to be checked against both. See issue #6.
        """
        data = self.client.get_json("/JSSResource/computergroups")
        return [(group["id"], group["name"], bool(group.get("is_smart")))
                for group in data.get("computer_groups", [])]

    def list_smart_groups(self):
        return [(group_id, name)
                for group_id, name, is_smart in self.list_all_groups() if is_smart]

    def fetch_group(self, group_id):
        return self.client.get_xml(f"/JSSResource/computergroups/id/{group_id}")

    def build_criteria(self, root, new_value=None, target_indexes=()):
        parts = ["<criteria>"]
        for index, criterion in enumerate(root.findall("./criteria/criterion")):
            parts.append("<criterion>")
            for field in CRITERION_FIELDS:
                value = criterion.findtext(field)
                if value is None:
                    continue
                if field == "value" and new_value is not None and index in target_indexes:
                    value = new_value
                parts.append(f"<{field}>{xml_escape(value)}</{field}>")
            parts.append("</criterion>")
        parts.append("</criteria>")
        return "".join(parts)

    def find_criteria_references(self, group_xml, group_name):
        """Return the indexes of criteria that reference the named group.

        Only criteria of type Computer Group are considered. Matching on the
        value alone rewrites unrelated criteria, such as Building or Department,
        whose value happens to equal the group name.

        search_type is deliberately not checked. The interface produces only
        "member of" and "not member of" for this criterion type, but a criterion
        created through the API may use something else, and missing a real
        reference is the failure this check exists to prevent.
        """
        root = ET.fromstring(group_xml)
        references = []
        for index, criterion in enumerate(root.findall("./criteria/criterion")):
            if (criterion.findtext("name") or "").strip() != GROUP_CRITERION_NAME:
                continue
            if (criterion.findtext("value") or "").strip() == group_name:
                references.append(index)
        return references

    def find_dependent_groups(self, group_id, group_name):
        dependents = []
        for candidate_id, candidate_name in self.list_smart_groups():
            if candidate_id == group_id:
                continue
            group_xml = self.fetch_group(candidate_id)
            references = self.find_criteria_references(group_xml, group_name)
            if references:
                dependents.append({"id": candidate_id, "name": candidate_name,
                                   "xml": group_xml, "references": references})
        return dependents

    def get_membership(self, group_id):
        root = ET.fromstring(self.fetch_group(group_id))
        return {computer.findtext("id") for computer in root.findall("./computers/computer")}

    def find_duplicate_names(self):
        duplicates = {}
        for label, endpoint, list_key, _ in SCOPED_TYPES:
            try:
                items = self.client.get_json(endpoint).get(list_key, [])
            except JamfError:
                continue
            seen = {}
            for item in items:
                seen.setdefault(item["name"], []).append(item["id"])
            for name, ids in seen.items():
                if len(ids) > 1:
                    duplicates[(label, name)] = ids
        return duplicates

    def find_scoped_objects(self, group_id):
        scoped = []
        for label, endpoint, list_key, root_tag in SCOPED_TYPES:
            try:
                items = self.client.get_json(endpoint).get(list_key, [])
            except JamfError:
                continue
            for item in items:
                try:
                    document = self.client.get_xml(f"{endpoint}/id/{item['id']}")
                except JamfError:
                    continue
                if not self._scope_references_group(document, group_id):
                    continue
                scoped.append({"type": label, "id": item["id"], "name": item["name"],
                               "endpoint": endpoint, "root_tag": root_tag,
                               "document": document})
        return scoped

    @staticmethod
    def _scope_element(document):
        try:
            root = ET.fromstring(document)
        except ET.ParseError:
            return None, None
        return root, root.find("./scope")

    def _scope_references_group(self, document, group_id):
        _, scope = self._scope_element(document)
        if scope is None:
            return False
        return any(entry.findtext("id") == str(group_id)
                   for entry in scope.iter("computer_group"))

    @staticmethod
    def build_site(root):
        """Rebuild the source group's site element.

        Without this the clone is created in no site, which changes who can see
        and scope it. See issue #5. Jamf Pro returns id -1 and name None for a
        group in no site, and accepts that back unchanged.
        """
        site = root.find("./site")
        if site is None:
            return ""
        parts = ["<site>"]
        for field in SITE_FIELDS:
            value = site.findtext(field)
            if value is None:
                continue
            parts.append(f"<{field}>{xml_escape(value)}</{field}>")
        parts.append("</site>")
        return "".join(parts)

    def create_group(self, name, source_xml):
        root = ET.fromstring(source_xml)
        body = (f"<computer_group><name>{xml_escape(name)}</name>"
                f"<is_smart>true</is_smart>{self.build_site(root)}"
                f"{self.build_criteria(root)}</computer_group>")
        status, response = self.client.post_xml("/JSSResource/computergroups/id/0", body)
        if status not in (200, 201):
            raise JamfError(f"Could not create the new group. {describe_failure(status, response)}")
        return ET.fromstring(response).findtext("id")

    def repoint_criteria(self, dependent, new_name):
        root = ET.fromstring(dependent["xml"])
        criteria = self.build_criteria(root, new_name, set(dependent["references"]))
        status, response = self.client.put_xml(
            f"/JSSResource/computergroups/id/{dependent['id']}",
            f"<computer_group>{criteria}</computer_group>")
        if status in (200, 201):
            return True, None
        return False, describe_failure(status, response)

    def repoint_scope(self, scoped_object, old_id, new_id, new_name):
        document = scoped_object["document"]
        start = document.find("<scope>")
        end = document.find("</scope>")
        if start < 0 or end < 0:
            return False, "the object has no scope section"
        end += len("</scope>")

        try:
            scope = ET.fromstring(document[start:end])
        except ET.ParseError as error:
            return False, f"the scope could not be read ({error})"

        updated = 0
        for entry in scope.iter("computer_group"):
            if entry.findtext("id") != str(old_id):
                continue
            identifier = entry.find("id")
            if identifier is None:
                continue
            identifier.text = str(new_id)
            label = entry.find("name")
            if label is None:
                label = ET.SubElement(entry, "name")
            label.text = new_name
            updated += 1

        if updated == 0:
            return False, "the group was not found in the scope when re-reading the object"

        scope_xml = ET.tostring(scope, encoding="unicode")
        tag = scoped_object["root_tag"]
        path = f"{scoped_object['endpoint']}/id/{scoped_object['id']}"
        object_name = self._object_name(document) or scoped_object["name"]

        # Jamf accepts different shapes depending on the object type. Configuration
        # profiles validate the name on update, so general must carry both the id and
        # the existing name. Policies accept a scope-only body. The payloads are never
        # resent, as Jamf treats a returned payload as a new profile and rejects it as
        # a duplicate.
        attempts = [
            ("general and scope",
             f"<{tag}><general><id>{scoped_object['id']}</id>"
             f"<name>{xml_escape(object_name)}</name></general>{scope_xml}</{tag}>"),
            ("scope only",
             f"<{tag}>{scope_xml}</{tag}>"),
            ("general id and scope",
             f"<{tag}><general><id>{scoped_object['id']}</id></general>{scope_xml}</{tag}>"),
        ]

        failures = []
        for label, body in attempts:
            status, response = self.client.put_xml(path, body)
            if status in (200, 201):
                return True, None
            failures.append((label, status, response))

        for label, status, response in failures:
            self._log_failure(scoped_object, status, response, label)
        label, status, response = failures[0]
        return False, describe_failure(status, response)

    @staticmethod
    def _object_name(document):
        try:
            root = ET.fromstring(document)
        except ET.ParseError:
            return None
        general = root.find("./general")
        return general.findtext("name") if general is not None else None

    @staticmethod
    def _log_failure(scoped_object, status, response, attempt=""):
        try:
            with open("jamf-rename-errors.log", "a", encoding="utf-8") as handle:
                handle.write(f"\n{'=' * 70}\n")
                handle.write(f"{datetime.now().isoformat()}\n")
                handle.write(f"{scoped_object['type']} \"{scoped_object['name']}\" "
                             f"(id {scoped_object['id']})\n")
                handle.write(f"attempt: {attempt}\n")
                handle.write(f"status {status}\n")
                handle.write((response or "")[:4000])
                handle.write("\n")
        except Exception:
            pass

    def delete_group(self, group_id):
        status, response = self.client.delete(f"/JSSResource/computergroups/id/{group_id}")
        if status in (200, 201):
            return True, None
        return False, describe_failure(status, response)


def prompt_required(message):
    while True:
        value = input(Style.info(message)).strip()
        if value:
            return value
        print(Style.warn("  A value is required."))


def prompt_choice(matches):
    print()
    for position, (_, name) in enumerate(matches, start=1):
        print(f"  {Style.faint(f'{position:>3}.')} {name}")
    selection = ask("\nSelect a number: ")
    if not selection.isdigit() or not 1 <= int(selection) <= len(matches):
        return None
    return matches[int(selection) - 1]


def prompt_new_name(current_name, existing_names):
    while True:
        new_name = ask(f"\nRename \"{current_name}\" to: ")
        if not new_name:
            print(Style.warn("  A name is required."))
            continue
        if new_name in existing_names:
            kind = existing_names[new_name]
            print(Style.warn(f"  A {kind} computer group named \"{new_name}\" "
                             f"already exists."))
            continue
        confirmation = ask("Type the new name again to confirm: ")
        if confirmation != new_name:
            print(Style.warn("  The names did not match."))
            continue
        return new_name


def write_backup(server_url, group_id, old_name, new_name, group_xml, dependents, scoped):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"jamf-rename-backup-{stamp}.json"
    payload = {
        "tool": f"{TOOL_NAME} v{VERSION}",
        "server": server_url,
        "timestamp": stamp,
        "group": {"id": group_id, "old_name": old_name, "new_name": new_name, "xml": group_xml},
        "dependent_groups": [{key: item[key] for key in ("id", "name", "xml")}
                             for item in dependents],
        "scoped_objects": [{key: item[key] for key in ("type", "id", "name", "document")}
                           for item in scoped],
    }
    with open(filename, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return filename


def rename_workflow(client):
    renamer = SmartGroupRenamer(client)

    heading("Select a group")
    with Spinner("loading computer groups"):
        all_groups = renamer.list_all_groups()
    smart_groups = [(group_id, name)
                    for group_id, name, is_smart in all_groups if is_smart]
    existing_names = {name: ("smart" if is_smart else "static")
                      for _, name, is_smart in all_groups}
    static_count = len(all_groups) - len(smart_groups)
    print(f"  {len(smart_groups)} smart computer groups found, "
          f"{static_count} static.")

    search = ask("\nSearch by name (blank for all): ").lower()
    matches = [(gid, name) for gid, name in smart_groups if search in name.lower()]
    if not matches:
        print(Style.warn("\n  Nothing matched that search."))
        return

    chosen = prompt_choice(matches)
    if not chosen:
        print(Style.warn("\n  That was not a valid selection."))
        return
    old_id, old_name = chosen

    new_name = prompt_new_name(old_name, existing_names)

    heading("Reviewing what refers to this group")
    with Spinner("searching smart group criteria"):
        dependents = renamer.find_dependent_groups(old_id, old_name)
    print(Style.strong("  Smart groups referencing it in their criteria"))
    print(draw_table(["Smart group", "References"],
                     [[item["name"], len(item["references"])] for item in dependents]))

    with Spinner("searching policies and configuration profiles"):
        scoped = renamer.find_scoped_objects(old_id)
    print()
    print(Style.strong("  Objects scoped to it"))
    print(draw_table(["Type", "Name"], [[item["type"], item["name"]] for item in scoped]))

    with Spinner("checking for duplicate object names"):
        duplicates = renamer.find_duplicate_names()
    shared = [item for item in scoped if (item["type"], item["name"]) in duplicates]
    if shared:
        print()
        print(Style.bad("  Cannot continue."))
        print(Style.bad(f"  \"{old_name}\" has not been renamed and nothing has been "
                        f"changed."))
        print()
        print("  The objects below are scoped to this group and share a name with")
        print("  another object of the same type. Jamf Pro will not accept any update")
        print("  to them, so they cannot be moved to the renamed group.")
        print(draw_table(["Type", "Name", "Objects sharing this name"],
                         [[item["type"], item["name"],
                           len(duplicates[(item["type"], item["name"])])]
                          for item in shared]))
        guidance_heading("Next steps")
        step = 1
        for item in shared:
            copies = len(duplicates[(item["type"], item["name"])])
            print(Style.warn(f"  {step}. In Jamf Pro, open each of the {copies} objects "
                             f"named \"{item['name']}\""))
            print(Style.warn(f"     and give all but one of them a different name."))
            step += 1
        print(Style.warn(f"  {step}. Run this tool again and repeat the rename."))
        print()
        return

    backup_file = write_backup(client.base_url, old_id, old_name, new_name,
                               renamer.fetch_group(old_id), dependents, scoped)
    print(f"\n  {TICK} Backup written to {Style.strong(backup_file)}")

    heading("Applying changes")
    failed = []
    blocked = False

    source_xml = renamer.fetch_group(old_id)
    new_id = renamer.create_group(new_name, source_xml)
    print(f"  {TICK} Created \"{new_name}\"")

    original_members = renamer.get_membership(old_id)
    new_members = set()
    with Spinner("waiting for membership to recalculate"):
        for _ in range(6):
            time.sleep(5)
            new_members = renamer.get_membership(new_id)
            if new_members == original_members:
                break

    if new_members == original_members:
        print(f"  {TICK} Membership matches, {len(original_members)} devices")
    else:
        print(f"  {CROSS} Membership differs. Original {len(original_members)}, "
              f"new {len(new_members)}")
        print(Style.warn(f"\n  Stopped before making any further change. \"{new_name}\" was "
                         f"created and nothing else was altered."))
        print(Style.warn(f"  Remove it manually if it is not wanted. Backup at {backup_file}"))
        return

    for dependent in dependents:
        succeeded, reason = renamer.repoint_criteria(dependent, new_name)
        label = f"Updated criteria in \"{dependent['name']}\""
        if succeeded:
            print(f"  {TICK} {label}")
        else:
            print(f"  {CROSS} {label}")
            print(Style.bad(f"      {reason}"))
            failed.append((label, reason))
            blocked = True

    for scoped_object in scoped:
        label = f"Re-scoped {scoped_object['type'].lower()} \"{scoped_object['name']}\""
        succeeded, reason = renamer.repoint_scope(scoped_object, old_id, new_id, new_name)
        if succeeded:
            print(f"  {TICK} {label}")
        else:
            print(f"  {CROSS} {label}")
            print(Style.bad(f"      {reason}"))
            print(Style.bad(f"      It is unchanged and still points at \"{old_name}\"."))
            failed.append((label, reason))
            blocked = True

    with Spinner("verifying no references remain"):
        remaining = [name for gid, name in renamer.list_smart_groups()
                     if gid != old_id
                     and renamer.find_criteria_references(renamer.fetch_group(gid), old_name)]
    for name in remaining:
        print(f"  {CROSS} \"{name}\" still references \"{old_name}\"")
        failed.append((f"Reference in \"{name}\"", "the criterion still points at the old name"))
        blocked = True

    delete_blockers = []
    if not blocked:
        succeeded, reason = renamer.delete_group(old_id)
        if succeeded:
            print(f"  {TICK} Removed \"{old_name}\"")
        else:
            delete_blockers = extract_dependencies(reason)
            print(f"  {CROSS} Could not remove \"{old_name}\"")
            print(Style.bad(f"      {reason}"))

    if failed:
        heading("Summary")
        for entry, reason in failed:
            print(f"  {CROSS} {entry}")
            print(Style.bad(f"      {reason}"))
        print()
        print(Style.bad("  The rename is incomplete."))
        print(f"  Both \"{old_name}\" and \"{new_name}\" now exist. The items marked "
              f"{CROSS} above")
        print(f"  still point at \"{old_name}\" and must be repointed by hand before it "
              f"can be removed.")
        print(Style.faint("\n  Full server responses recorded in jamf-rename-errors.log"))
        print(Style.faint(f"  Backup retained at {backup_file}"))

    elif delete_blockers:
        print()
        print(Style.warn(f"  \"{new_name}\" has been created and everything this tool "
                         f"can reach"))
        print(Style.warn(f"  now points at it, but \"{old_name}\" could not be removed "
                         f"because"))
        print(Style.warn("  something is still using it. Both groups currently exist."))
        print(Style.faint(f"\n  Backup retained at {backup_file}"))

        guidance_heading("Next steps")
        listed = ", ".join(delete_blockers)
        print(Style.warn(f"  1. In Jamf Pro, open the {listed} still using "
                         f"\"{old_name}\""))
        print(Style.warn(f"     and change the scope to \"{new_name}\"."))
        print(Style.warn(f"  2. Delete \"{old_name}\"."))
        if any("blueprint" in item.lower() for item in delete_blockers):
            print(Style.warn("\n     Blueprint scopes are held in a separate service and "
                             "cannot be"))
            print(Style.warn("     updated by this tool."))
        return

    else:
        print()
        print(Style.ok(f"  {TICK} \"{old_name}\" was successfully renamed to "
                       f"\"{new_name}\"."))
        print(Style.faint(f"\n  Backup retained at {backup_file}"))


def main():
    banner()
    heading("Connect")

    server_url = prompt_required("Jamf Pro URL (e.g. https://yourserver.jamfcloud.com): ")
    if not server_url.startswith("http"):
        server_url = "https://" + server_url

    print()
    print("  1. API client   (client ID and secret, works with single sign on)")
    print("  2. User account (username and password)")
    choice = input(Style.info("\nAuthentication method [1]: ")).strip() or "1"

    if choice == "2":
        credentials = {
            "method": "user",
            "username": prompt_required("Username: "),
            "password": getpass.getpass(Style.info("Password: ")),
        }
    else:
        credentials = {
            "method": "client",
            "client_id": prompt_required("Client ID: "),
            "client_secret": getpass.getpass(Style.info("Client secret: ")),
        }

    try:
        client = JamfClient(server_url, credentials)
    except JamfError as error:
        print(f"\n  {CROSS} {Style.bad(str(error))}\n")
        sys.exit(1)

    print(f"\n  {TICK} Connected to {Style.strong(server_url)}")

    try:
        while True:
            try:
                rename_workflow(client)
                again = ask("\nRename another group? (yes/[NO]): ").lower()
                if again not in ("y", "yes"):
                    break
            except JamfError as error:
                print(f"\n  {CROSS} {Style.bad(str(error))}")
                break
            except InputTimeout as expired:
                report_timeout(expired)
                break
    finally:
        client.logout()
        print(Style.faint("\nSession closed.\n"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(Style.warn("\n\nCancelled.\n"))
    except InputTimeout as expired:
        report_timeout(expired)
        print()
