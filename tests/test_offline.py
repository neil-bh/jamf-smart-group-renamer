#!/usr/bin/env python3
"""
Offline checks for Jamf Smart Group Renamer.

Exercises the pure XML logic with no Jamf Pro instance and no credentials.
Covers issues #3 and #5. Issue #6 needs a live instance and is not covered here.

Run from the directory holding jamf_smart_group_renamer.py:

    python3 test_offline.py
"""

import sys
import xml.etree.ElementTree as ET

import jamf_smart_group_renamer as jsgr

RENAMER = jsgr.SmartGroupRenamer.__new__(jsgr.SmartGroupRenamer)

FAILURES = []


def check(label, actual, expected):
    if actual == expected:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        print(f"          expected: {expected!r}")
        print(f"          actual:   {actual!r}")
        FAILURES.append(label)


def criterion(name, search_type, value, priority):
    return (f"<criterion><name>{name}</name><priority>{priority}</priority>"
            f"<and_or>and</and_or><search_type>{search_type}</search_type>"
            f"<value>{value}</value><opening_paren>false</opening_paren>"
            f"<closing_paren>false</closing_paren></criterion>")


def group(criteria, site=None):
    site_xml = "" if site is None else f"<site><id>{site[0]}</id><name>{site[1]}</name></site>"
    return (f"<computer_group><id>50</id><name>Dependent</name>"
            f"<is_smart>true</is_smart>{site_xml}"
            f"<criteria>{''.join(criteria)}</criteria></computer_group>")


def criteria_values(root, new_value, targets):
    rebuilt = ET.fromstring("<computer_group>"
                            + RENAMER.build_criteria(root, new_value, targets)
                            + "</computer_group>")
    return [(item.findtext("name"), item.findtext("value"))
            for item in rebuilt.findall("./criteria/criterion")]


print("\nIssue #3, criteria references")

only_group = group([criterion("Computer Group", "member of", "MAC - Ring 1", 0)])
check("a single group reference is found",
      RENAMER.find_criteria_references(only_group, "MAC - Ring 1"), [0])

not_member = group([criterion("Computer Group", "not member of", "MAC - Ring 1", 0)])
check("not member of is treated as a reference",
      RENAMER.find_criteria_references(not_member, "MAC - Ring 1"), [0])

# The v0.2 regression. Matching on value alone returned both indexes.
mixed = group([
    criterion("Computer Name", "is", "MAC - Ring 1", 0),
    criterion("Computer Group", "member of", "MAC - Ring 1", 1),
    criterion("Computer Group", "member of", "MAC - Ring 9", 2),
])
check("a non-group criterion holding the same value is ignored",
      RENAMER.find_criteria_references(mixed, "MAC - Ring 1"), [1])

check("a group reference to a different group is ignored",
      RENAMER.find_criteria_references(mixed, "MAC - Ring 4"), [])

check("only the group criterion is rewritten",
      criteria_values(ET.fromstring(mixed), "MAC - Ring 1 OLD", {1}),
      [("Computer Name", "MAC - Ring 1"),
       ("Computer Group", "MAC - Ring 1 OLD"),
       ("Computer Group", "MAC - Ring 9")])

partial = group([criterion("Computer Group", "like", "MAC - Ring", 0)])
check("like is not yet detected, see issue #4",
      RENAMER.find_criteria_references(partial, "MAC - Ring 1"), [])

padded = group([criterion("Computer Group", "member of", " MAC - Ring 1 ", 0)])
check("surrounding whitespace is tolerated",
      RENAMER.find_criteria_references(padded, "MAC - Ring 1"), [0])

print("\nIssue #5, site")

in_site = ET.fromstring(group([], site=("3", "UK")))
check("a real site is carried across",
      RENAMER.build_site(in_site), "<site><id>3</id><name>UK</name></site>")

no_site = ET.fromstring(group([], site=("-1", "None")))
check("the no site shape is echoed back",
      RENAMER.build_site(no_site), "<site><id>-1</id><name>None</name></site>")

absent = ET.fromstring(group([]))
check("an absent site element produces nothing",
      RENAMER.build_site(absent), "")

print("\nBuild the clone body")

source = group([criterion("Computer Group", "member of", "MAC - Ring 9", 0)],
               site=("3", "UK"))
root = ET.fromstring(source)
body = (f"<computer_group><name>{jsgr.xml_escape('MAC - Ring 1')}</name>"
        f"<is_smart>true</is_smart>{RENAMER.build_site(root)}"
        f"{RENAMER.build_criteria(root)}</computer_group>")
parsed = ET.fromstring(body)
check("the clone body carries name, is_smart, site and criteria",
      [parsed.findtext("name"), parsed.findtext("is_smart"),
       parsed.findtext("./site/name"),
       len(parsed.findall("./criteria/criterion"))],
      ["MAC - Ring 1", "true", "UK", 1])

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) failed.\n")
    sys.exit(1)
print("All checks passed.\n")
