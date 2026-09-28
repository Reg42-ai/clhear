# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""CLHEAR settings.

# ARCH: the host service has its own settings module and conventions; this standalone
# settings object mirrors what the HLD names (CLHEAR_ENABLED feature
# flag, spend caps, queue/bucket wiring) so it can be folded into the existing
# settings when this package moves into the host service.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    clhear_enabled: bool = True

    # Postgres in AWS (Aurora); sqlite fallback keeps dev/tests offline.
    database_url: str = "sqlite:///./clhear.db"

    aws_region: str = "us-east-1"
    clhear_events_queue_url: str = ""
    clhear_events_dlq_url: str = ""
    clhear_datalake_bucket: str = ""

    # The consumer configures one model provider. Empty means a live run cannot start.
    # ``fake`` is the offline quickstart only.
    clhear_llm_provider: str = ""  # fake | anthropic | openai_compatible | bedrock
    clhear_llm_model: str = ""
    anthropic_api_key: str = ""
    openai_base_url: str = ""
    openai_api_key: str = ""
    bedrock_model_id: str = ""
    # Kept so older call sites that still read these attributes do not crash. Unused.
    infer_base_url: str = ""
    infer_token: str = ""
    infer_employee_id: str = "clhear"
    infer_data_class: str = "public"
    # Hard caps (§5): alarm handled by CloudWatch on the llm spend metric.
    clhear_gateway_fleet_daily_cap_usd: float = 20.0
    clhear_gateway_global_daily_cap_usd: float = 100.0
    clhear_frontier_monthly_cap_usd: float = 50.0  # premium rungs (Opus-class) per month

    # Fidelity gate + repair loop (evals are gates, not reports).
    clhear_fidelity_threshold: float = 0.995
    clhear_ingest_max_attempts: int = 3
    # Max share of tokens dumb salvage may recover as unstructured notes;
    # bigger gaps need typed hints (learned or LLM-proposed) or the run fails.
    clhear_salvage_cap: float = 0.02
    clhear_model_repair: str = ""  # empty = the router's l1_parse ladder decides

    # ARCH: stand-in for the host service auth; comma-separated identities with the
    # `maintainer` role. Replace with the existing session/role dependency on merge.
    clhear_maintainers: str = ""
    # Private review deployment. Authentication is checked before any corpus API,
    # not merely before rendering HTML. This never grants publisher text rights.
    clhear_restricted_access: bool = False
    clhear_reviewer_emails: str = ""
    # "accounts": any verified, non-suspended account may use the web app, and
    # /v1 with a key; the reviewer and maintainer allowlists guard only review,
    # write and admin routes. Empty keeps reviewer-only mode under restricted access.
    clhear_access_mode: str = ""
    # Header value the CloudFront edge sends; requests without it are refused.
    clhear_origin_verify_secret: str = ""
    clhear_terms_version: str = "2026-09-25"
    clhear_rate_v1_per_minute: int = 600
    clhear_rate_account_per_minute: int = 1200
    clhear_rate_signup_per_ip_15m: int = 10
    clhear_rate_magic_link_per_email_15m: int = 3
    clhear_max_body_bytes: int = 1_000_000
    clhear_cors_origins: str = ""  # comma-separated exact origins, e.g. https://example.invalid
    clhear_l1_only: bool = False  # enable on worker deployments while accepting L1
    # Private UI iteration over a worker-generated snapshot, never a writer.
    clhear_preview_mode: bool = False
    clhear_preview_snapshot_path: str = ""  # local only; Lambda uses its synchronized snapshot
    clhear_preview_snapshot_s3_uri: str = ""  # optional local refresh through the existing reader synchronizer

    # Exporter target: local checkout dir and optional remote (public `clhear` repo).
    clhear_public_repo_dir: str = "./clhear-public"
    clhear_public_repo_url: str = ""
    clhear_export_git_token: str = ""
    # HLD v2 §9: no public disclosure before the provisional filing is confirmed.
    # The exporter compiles the public repo locally regardless; it pushes only when
    # this is true (set as a repository variable in the release workflow).
    clhear_public_disclosure_confirmed: bool = False
    # HLD v2 I9: agnostic (public product) | member | instance (runs in a client account).
    clhear_mode: str = "agnostic"
    # L8 benchmark inputs are keyed by HMAC(member id, this secret); rotate = new cohort history.
    clhear_benchmark_hmac_key: str = ""

    # HLD v2 §6 tooling: Discourse forum (link shown on the contribute page) and the
    # beehiiv newsletter that carries the change digest. Empty = hooks are inert.
    clhear_discourse_url: str = ""
    clhear_beehiiv_api_key: str = ""
    clhear_beehiiv_publication_id: str = ""  # pub_…
    clhear_beehiiv_post_status: str = "draft"  # draft | confirmed (confirmed sends immediately)

    clhear_artifacts_dir: str = "./artifacts"
    clhear_service_tokens: str = ""
    clhear_service_token_file: str = ""
    clhear_bind_host: str = "127.0.0.1"
    clhear_engine_version: str = "0.1.0"
    clhear_api_version: str = "v1"
    clhear_schema_revision: str = "0041"
    clhear_image_digest: str = ""

    # --- community accounts & contributions (Phase C) ---
    # HMAC key for session cookies + magic-link tokens. MUST be set in prod
    # (SSM /clhear/SESSION_SECRET). Tests supply their own isolated secret.
    clhear_session_secret: str = ""
    clhear_auth_debug: bool = False  # dev: return magic links in the response
    clhear_ses_sender: str = "CLHEAR <noreply@127.0.0.1>"
    clhear_public_base_url: str = "http://127.0.0.1:8000"
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    apple_oauth_client_id: str = ""  # Service ID; Apple flow activates when set
    apple_oauth_team_id: str = ""
    apple_oauth_key_id: str = ""
    apple_oauth_private_key: str = ""
    clhear_submissions_daily_limit: int = 10
    # HLD v2 §5 identity: Cognito user pool (Google IdP for Reg42). Empty =
    # Cognito off; magic link + direct Google OAuth keep working.
    clhear_cognito_region: str = ""
    clhear_cognito_user_pool_id: str = ""
    clhear_cognito_client_id: str = ""
    clhear_cognito_domain: str = ""  # hosted UI, https://<prefix>-auth.auth.<region>.amazoncognito.com
    # HLD v2 §7.1 enterprise SSO: SAML IdPs federated into the pool, as a JSON map
    # {"AcmeBank": ["acme.example", "acme-group.example"]} (infra/cognito.tf publishes it).
    # /auth/sso?email= picks the IdP by domain; empty = no enterprise SSO.
    clhear_saml_domains: str = ""

    @property
    def saml_domain_map(self) -> dict[str, str]:
        """lower-case email domain -> Cognito SAML provider name."""
        import json

        try:
            raw = json.loads(self.clhear_saml_domains) if self.clhear_saml_domains.strip() else {}
        except ValueError:
            return {}
        out: dict[str, str] = {}
        for name, domains in (raw or {}).items():
            for d in domains if isinstance(domains, list) else [domains]:
                out[str(d).strip().lower().lstrip("@")] = str(name)
        return out

    # HLD v2 I7 projections: Neo4j query graph (empty = in-process projection) and
    # the clause embedding index (auto = Infer when configured, else hash-v1).
    clhear_neo4j_uri: str = ""  # bolt://clhear-neo4j.clhear.local:7687
    clhear_neo4j_user: str = "neo4j"
    clhear_neo4j_password: str = ""
    clhear_embedding_provider: str = "auto"  # auto | infer | hash
    clhear_embedding_model: str = "amazon.titan-embed-text-v2:0"

    # Snapshot mode for the scheduled fleet: the corpus SQLite lives in S3
    # (same object the public explorer serves); workers pull it, ingest, and
    # publish it back. Empty = use database_url directly (Aurora / local dev).
    clhear_snapshot_s3_uri: str = ""

    # Consumer API keys: "app_id:secret" or "app_id:secret:read:l1+read:l2"
    clhear_app_keys: str = ""

    # Named releases live under this prefix (s3://bucket/releases/...).
    # Empty = local artifacts dir (dev/tests).
    clhear_releases_s3_prefix: str = ""

    @property
    def maintainer_set(self) -> set[str]:
        return {m.strip() for m in self.clhear_maintainers.split(",") if m.strip()}

    @property
    def reviewer_set(self) -> set[str]:
        configured = self.clhear_reviewer_emails or self.clhear_maintainers
        return {email.strip().lower() for email in configured.split(",") if email.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
