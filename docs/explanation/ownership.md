# Ownership of the framework and the apps

This page explains who is responsible for which part of PMM Extensions, and what that means for someone who builds an app.

## Two kinds of code

PMM Extensions has two kinds of code, and they have different owners.

The framework is everything an app stands on: the task engine, the inventory, the form DSL, the derived routes, the sidecar image and its settings. The PMM team owns it. A change to the framework affects every app, so the PMM team decides what goes in.

An app is one package under `app/extensions/apps/`, with its tests. Whoever builds an app owns it. The owner decides what the app does, keeps it working as the framework changes, and answers questions about it.

## What ownership means for an app builder

An app builder is responsible for the whole route of the app, not only for its Python package:

- the app's code and its tests
- the change in PMM's UI that makes the app visible
- the tools that the app expects on a database host, and the documentation that says so
- fixes, when a user reports a problem with the app

The framework gives every app the same behavior, with no code in the app: routes, validation, task storage, logs and the form. An app builder does not maintain those.

## How a change is reviewed

Every pull request is reviewed by the owners of the paths it changes. The file `.github/CODEOWNERS` maps each path to its reviewers, and GitHub requests their review when a pull request opens.

Two rules follow from that file in most cases:

- A change inside an app's package needs a review from the backend reviewers.
- A change to the framework, to the dependencies or to the build needs a review from the core reviewers. Some of those paths need more than one approval.

The file has exceptions, and it decides. Three framework modules go to the backend reviewers: the conformance checks, the list queries and the scaffolder.

A pull request that adds an app and also changes the framework is reviewed by both groups. Keep the two in separate pull requests when you can: the app then does not wait for the framework review.

## What the PMM team does today

For now, the PMM team reviews every app and decides which apps ship in the image. It can also work directly on an app that ships.

The PMM team supports app builders in two ways. It answers questions about the framework, and it improves the framework when an app shows a gap. If an app needs something the framework cannot do, ask for the framework change. Do not work around it inside the app.

[The app status page](../app-status.md) lists the apps that ship and the state of the others.

## How apps reach users

An app reaches users inside a PMM release. The image that PMM ships holds the apps listed in `sidecar/settings.yaml`, and PMM's UI has an entry for each one that has pages. [Get an app into a PMM release](../how-to/get-an-app-into-a-pmm-release.md) describes the steps.

A marketplace for apps is a planned direction.

## Where to ask

[Contributing](../../CONTRIBUTING.md) says where to ask a question and where to report a problem.
