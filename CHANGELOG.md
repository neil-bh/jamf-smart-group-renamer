# Changelog

## 0.3 (unreleased)

- Criteria references are now matched only on criteria of type Computer Group.
  Matching on the value alone rewrote unrelated criteria, such as Computer Name
  or Building, whose value happened to equal the group name ([#3](../../issues/3))
- The cloned group now carries the site of the original. It was previously
  created in no site ([#5](../../issues/5))
- A new name is now checked against every computer group rather than smart groups
  only, so a clash with a static group is rejected at the prompt instead of
  failing later at creation ([#6](../../issues/6))
- Added `tests/test_offline.py`, which exercises the criteria and site logic with
  no Jamf Pro instance and no credentials
  
## 0.2

- Added API client authentication using a client ID and secret. Where Jamf Pro
  console login uses single sign on, user credentials cannot be used against the
  API, so this was previously unusable on those instances ([#2](../../issues/2))
- The API client is now the default method at startup. Username and password remains
  available for instances using local Jamf Pro accounts
- Tokens are renewed automatically when they expire mid run. API client tokens are
  short lived, so a longer run previously failed partway through with a 401

## 0.1

First release.

- Renames a smart computer group by cloning it under the new name, repointing every
  reference, then removing the original, so no dependent group is ever left without
  its criteria
- Repoints smart group criteria that reference the renamed group
- Re-scopes policies and configuration profiles onto the new group
- Writes a full backup before any change
- Verifies membership matches before repointing anything
- Stops before making any change when a scoped object shares its name with another
  object of the same type, as the Jamf Pro Classic API refuses to update those
- Reports a blocked deletion as its own outcome, naming the dependency Jamf reports
- Prompts time out after one minute once authenticated
- Standard library only, nothing to install
