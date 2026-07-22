# GitHub Desktop setup on macOS

## Add the prepared repository

1. Extract the repository ZIP on the Mac.
2. Open GitHub Desktop and sign in to the intended GitHub account.
3. Confirm that the signed-in account is `laurefisika`.
4. Select **File → Add Local Repository**.
5. Choose the extracted `npg-chamber-control` folder.
6. Confirm that GitHub Desktop detects the existing repository and its history.

Do not use **Create New Repository** inside a subfolder of this project; that would produce a nested repository and lose the prepared history.

## Publish it privately

1. Click **Publish repository**.
2. Keep the name `npg-chamber-control` unless a different professional name has been chosen.
3. Write a short description such as: `Control and automation software for a four-phase UHV nanoporous graphene synthesis workflow.`
4. Keep **Keep this code private** selected.
5. Choose the personal GitHub account as owner and publish.

The first remote repository should remain private because the current archive includes institution-specific control software and documentation. Do not later switch this same repository to public; create a separate reviewed public export.

## Normal update workflow

1. Open the repository in GitHub Desktop.
2. Review the changed-file list before committing.
3. Use a short English summary such as `docs: add hardware acceptance notes`.
4. Click **Commit to main**.
5. Click **Push origin**.

Never commit `Data Samples`, local modes, sample names, experimental CSV files, credentials, or equipment passwords. The supplied `.gitignore` blocks the common generated paths, but the changed-file list must still be reviewed manually.
