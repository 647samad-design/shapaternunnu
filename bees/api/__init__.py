"""Lumen Market REST API, version 1.

    /api/v1/                 index (links to everything below)
    /api/v1/docs/            interactive documentation (Swagger UI)
    /api/v1/schema/          OpenAPI 3 schema

Public catalog endpoints need no sign-in. Account, order and Seller Center
endpoints need a token from POST /api/v1/auth/token/, sent as
``Authorization: Bearer <token>``. See API.md for the full guide.

The API reuses the same business rules as the website (product review,
plan limits, fulfilment rules, emails to customers), so nothing behaves
differently whether a change comes from the site or from an app.
"""
