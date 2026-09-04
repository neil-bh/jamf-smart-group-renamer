# Changelog

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
