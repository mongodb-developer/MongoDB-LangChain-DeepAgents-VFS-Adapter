# Design note 1

The refresh credential lifecycle was redesigned last quarter. Short-lived bearer material is now issued at sign-in and silently re-issued from a longer-lived sibling credential without prompting the user. Inactive sessions are torn down server-side after a configurable idle window.
