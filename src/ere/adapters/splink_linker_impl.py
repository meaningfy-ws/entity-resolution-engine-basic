"""Splink-backed similarity linker adapter (concrete implementation of SimilarityLinker port)."""

from __future__ import annotations

import logging
import threading

import duckdb
import pandas as pd
from splink import Linker, SettingsCreator, block_on
import splink.comparison_library as cl
from splink.backends.duckdb import DuckDBAPI

from ere.models.resolver import Mention, MentionId, MentionLink
from ere.models.ports.linker import SimilarityLinker

log = logging.getLogger(__name__)


def build_tf_df(mentions: list[Mention], entity_fields: list[str]) -> pd.DataFrame:
    """
    Convert a list of Mention objects to a TF DataFrame suitable for Splink's initial_df.

    Empty list produces a zero-row DataFrame with pd.StringDtype() columns (required to avoid
    DuckDB integer-inference bug on empty DataFrames).

    Args:
        mentions: List of Mention objects to include in the search space.
        entity_fields: List of field names to extract from mentions (e.g. ["legal_name", "country_code"]).

    Returns:
        DataFrame with columns: mention_id, entity_fields..., __splink_salt.
    """
    cols = ["mention_id"] + entity_fields

    if not mentions:
        # Empty DataFrame: use pd.StringDtype() to avoid DuckDB integer-inference bug
        schema = {c: pd.array([], dtype=pd.StringDtype()) for c in cols}
        schema["__splink_salt"] = pd.array([], dtype="float64")
        return pd.DataFrame(schema)

    # Non-empty: build from mention data
    rows = []
    for mention in mentions:
        flat_dict = mention.to_flat_dict()
        row = {
            "mention_id": flat_dict["mention_id"],
            **{
                f: flat_dict.get(f) or "" for f in entity_fields
            },  # Convert None to empty string
            "__splink_salt": 0.5,
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    # Explicitly cast all entity field columns to StringDtype to prevent DuckDB type inference issues
    for col in entity_fields:
        if col in df.columns:
            df[col] = df[col].astype(pd.StringDtype())

    return df


class SpLinkSimilarityLinker(SimilarityLinker):
    """
    Splink-backed implementation of SimilarityLinker port.

    Wraps Splink's Linker and maintains:
    - _splink_con: in-memory DuckDB connection (Splink temporary tables only)
    - _db_api: DuckDBAPI for Splink operations
    - _tf_df: in-memory DataFrame (search space of registered mentions)
    - _linker: Splink Linker instance

    Supports warm starts via initial_df parameter and incremental registration of new mentions.
    """

    def __init__(
        self,
        entity_fields: list[str],
        config: dict,
        initial_df: pd.DataFrame | None = None,
    ) -> None:
        """
        Initialize the Splink linker.

        Args:
            entity_fields: List of field names (e.g. ["legal_name", "country_code"]).
            config: Full resolver configuration dict (needs match_weight_threshold and splink section).
            initial_df: Pre-built TF DataFrame for warm starts; None means fresh (empty) start.
        """
        self._entity_fields = entity_fields
        self._config = config
        self._match_weight_threshold = config.get("match_weight_threshold", -10)

        # In-memory connection for Splink operations (avoids file I/O)
        self._splink_con = duckdb.connect()
        self._db_api = DuckDBAPI(connection=self._splink_con)

        # Initialize TF DataFrame from parameter or empty
        if initial_df is not None:
            self._tf_df = initial_df.copy()
        else:
            self._tf_df = build_tf_df([], entity_fields)

        # Create and initialize Splink linker
        settings = self._build_settings()
        self._linker = Linker(self._tf_df, settings, db_api=self._db_api)
        # Always register even when empty so Splink's cache has correct schema
        self._linker.table_management.register_table_input_nodes_concat_with_tf(
            self._tf_df, overwrite=True
        )

        # Apply cold-start parameters (before training)
        self._apply_cold_start_params()

        # Threading synchronization for safe linker swaps during training
        self._linker_swap_lock = threading.Lock()
        self._training_in_progress = threading.Event()

    def find_matches(self, mention: Mention) -> list[MentionLink]:
        """
        Score a mention against previously registered mentions.

        Returns all mention-links above match_weight_threshold (including below-threshold
        links needed for candidate discovery).

        Filters self-links (left_id == right_id) which can occur during warm-start
        when the mention already exists in the search space.

        Args:
            mention: The Mention to score against the search space.

        Returns:
            List of MentionLink objects (empty if no matches or search space is empty).
        """
        # Grab a local reference to the linker under lock to ensure we don't hold
        # the lock while Splink is running (which could block training threads).
        with self._linker_swap_lock:
            linker = self._linker

        # Log mention data being sent to Splink
        mention_dict = mention.to_flat_dict()
        log.trace(
            "find_matches: Comparing mention %s with %d records in search space. "
            "Mention data: %s, Blocking rules: %s, Match weight threshold: %.2f",
            mention.id.value,
            len(self._tf_df),
            mention_dict,
            [str(r) for r in self._get_blocking_rules()],
            self._match_weight_threshold,
        )

        # Convert None values to empty strings to prevent DuckDB type inference issues
        # (None values can be inferred as INTEGER, causing type mismatches in comparisons)
        mention_dict = {k: (v or "") for k, v in mention_dict.items()}

        # Splink's find_matches_to_new_records expects a list of dicts
        df = linker.inference.find_matches_to_new_records(
            [mention_dict],
            blocking_rules=self._get_blocking_rules(),
            match_weight_threshold=self._match_weight_threshold,
        ).as_pandas_dataframe()

        if df.empty:
            log.trace(
                "find_matches: No matches found for mention %s (search space empty or no matches above threshold)",
                mention.id.value,
            )
            return []

        log.trace(
            "find_matches: Splink returned %d matches for mention %s. Available columns: %s",
            len(df),
            mention.id.value,
            list(df.columns),
        )

        # Build MentionLink objects, filtering self-links
        links = []
        for idx, row in df.iterrows():
            left_id = MentionId(value=str(row["mention_id_l"]))
            right_id = MentionId(value=str(row["mention_id_r"]))
            score = float(row["match_probability"])

            # Skip self-links (can occur in warm-start scenarios)
            if left_id == right_id:
                log.trace(
                    "find_matches: Skipping self-link for mention %s",
                    mention.id.value,
                )
                continue

            # Log ALL row data for all pairs (critical for debugging the two-score bug)
            log.trace(
                "find_matches: Row %d - Mention %s vs %s: ALL DATA: %s",
                idx,
                left_id.value[:16],
                right_id.value[:16],
                dict(row),
            )

            # Extract detailed comparison scores
            # FIXME to be deleted
            jw_score = row.get("jaro_winkler_legal_name", None)
            country_match = row.get("exact_match_country_code", None)
            match_weight = row.get("match_weight", None)

            log.trace(
                "find_matches: Mention %s vs %s: "
                "match_probability=%.6f, match_weight=%.4f, "
                "jaro_winkler_legal_name=%s, exact_match_country_code=%s",
                left_id.value[:16],
                right_id.value[:16],
                score,
                float(match_weight) if match_weight else 0.0,
                jw_score,
                country_match,
            )

            links.append(MentionLink(left_id=left_id, right_id=right_id, score=score))

        log.trace(
            "find_matches: Returning %d links for mention %s",
            len(links),
            mention.id.value,
        )

        return links

    def register_mention(self, mention: Mention) -> None:
        """
        Add a mention to the search space for future find_matches() calls.

        Appends the mention to the TF DataFrame and re-registers it with Splink.
        Uses tf_incremental strategy (append only, no reload from database).

        Args:
            mention: The Mention to add to the search space.
        """
        flat_dict = mention.to_flat_dict()

        log.trace(
            "register_mention: Adding mention %s to search space. Data: %s. "
            "Current search space size: %d",
            mention.id.value,
            flat_dict,
            len(self._tf_df),
        )

        # Build new row with same schema as _tf_df
        new_row = pd.DataFrame(
            [
                {
                    "mention_id": flat_dict["mention_id"],
                    **{f: flat_dict.get(f) for f in self._entity_fields},
                    "__splink_salt": 0.5,
                }
            ]
        )

        # Cast string columns to pd.StringDtype() to prevent type drift on None values
        for col in self._entity_fields:
            if col in new_row.columns:
                new_row[col] = new_row[col].astype(pd.StringDtype())

        # Append to search space
        self._tf_df = pd.concat([self._tf_df, new_row], ignore_index=True)

        log.trace(
            "register_mention: Mention %s registered. New search space size: %d",
            mention.id.value,
            len(self._tf_df),
        )

        # Re-register with Splink
        self._linker.table_management.register_table_input_nodes_concat_with_tf(
            self._tf_df, overwrite=True
        )

    def train(self) -> None:
        """
        Estimate model parameters via EM (non-blocking, thread-safe).

        Safe to call multiple times (retraining is idempotent). Prevents concurrent
        training runs via _training_in_progress event.

        Uses copy-then-swap pattern: snapshots current TF DataFrame, trains on a new
        Linker instance, then swaps under lock. This allows find_matches() calls to
        proceed with the current linker while training happens asynchronously.

        Training failures (e.g., insufficient data for convergence) are caught silently,
        leaving cold-start defaults intact.
        """
        # Prevent concurrent training runs
        if self._training_in_progress.is_set():
            return
        self._training_in_progress.set()
        try:
            self._train_safe()
        finally:
            self._training_in_progress.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_blocking_rules(self) -> list:
        """Build Splink blocking rule objects from config."""
        rules = []
        for rule in self._config["splink"]["blocking_rules"]:
            fields = rule if isinstance(rule, list) else [rule]
            rules.append(block_on(*fields))
        return rules

    def _build_settings(self) -> SettingsCreator:
        """Translate the config dict into a Splink SettingsCreator."""
        splink_cfg = self._config["splink"]

        log.trace(
            "_build_settings: Building Splink settings. Entity fields: %s",
            self._entity_fields,
        )

        comparisons = []
        for comp in splink_cfg["comparisons"]:
            if comp["type"] == "jaro_winkler":
                thresholds = comp.get("thresholds", [0.9, 0.8])
                log.trace(
                    "_build_settings: Adding JaroWinkler comparison on field '%s' with thresholds %s",
                    comp["field"],
                    thresholds,
                )
                comparisons.append(
                    cl.JaroWinklerAtThresholds(comp["field"], thresholds)
                )
            elif comp["type"] == "exact_match":
                log.trace(
                    "_build_settings: Adding ExactMatch comparison on field '%s'",
                    comp["field"],
                )
                comparisons.append(cl.ExactMatch(comp["field"]))
            else:
                raise ValueError(f"Unknown comparison type: {comp['type']!r}")

        blocking_rules = self._get_blocking_rules()
        log.trace(
            "_build_settings: Blocking rules: %s",
            [str(r) for r in blocking_rules],
        )

        kwargs = {
            "link_type": "dedupe_only",
            "unique_id_column_name": "mention_id",
            "comparisons": comparisons,
            "blocking_rules_to_generate_predictions": blocking_rules,
        }
        prior = self._config["splink"].get("probability_two_random_records_match")
        if prior is not None:
            kwargs["probability_two_random_records_match"] = prior
            log.trace(
                "_build_settings: Prior probability (P(match)): %.4f",
                prior,
            )

        return SettingsCreator(**kwargs)

    def _get_em_training_rule(self):
        """
        Derive the EM training rule from config.

        Uses the first blocking rule, extracting the first field if it's a list
        (compound rule). This ensures EM training matches the blocking strategy.

        For config.yaml: first rule is "country_code" → block_on("country_code")
        For config_compound.yaml: first rule is [country_code, city] → block_on("country_code")
        For config_multirule.yaml: first rule is "country_code" → block_on("country_code")
        """
        first_rule = self._config["splink"]["blocking_rules"][0]
        em_field = first_rule[0] if isinstance(first_rule, list) else first_rule
        return block_on(em_field)

    def _train_safe(self) -> None:
        """
        Thread-safe training via copy-then-swap pattern.

        1. Snapshot the current TF DataFrame (may grow during training).
        2. Create a new Linker on a fresh in-memory DuckDB connection.
        3. Run EM training on the new linker.
        4. Re-register the current (possibly grown) TF DataFrame.
        5. Swap the linker reference under lock.

        Training failures are caught silently, leaving cold-start defaults intact.
        """
        try:
            # Snapshot current TF DataFrame at training start
            tf_df_snapshot = self._tf_df.copy()
            mention_count = len(tf_df_snapshot)

            log.info(
                "EM training STARTING: %d mentions available for parameter estimation",
                mention_count,
            )

            # Create new linker on fresh in-memory connection (no shared state)
            splink_con_new = duckdb.connect()
            db_api_new = DuckDBAPI(connection=splink_con_new)
            settings = self._build_settings()
            linker_new = Linker(tf_df_snapshot, settings, db_api=db_api_new)
            linker_new.table_management.register_table_input_nodes_concat_with_tf(
                tf_df_snapshot, overwrite=True
            )

            # Run EM training on the new linker
            log.info("EM training: estimating u-probabilities via random sampling")
            linker_new.training.estimate_u_using_random_sampling(max_pairs=1e6)

            log.info(
                "EM training: estimating m-probabilities and lambda via EM algorithm"
            )
            linker_new.training.estimate_parameters_using_expectation_maximisation(
                self._get_em_training_rule(), estimate_without_term_frequencies=True
            )

            # Extract trained parameters for logging (final state confirmation)
            self._log_trained_parameters(linker_new)

            # Re-register current TF DataFrame (which may have grown during training)
            linker_new.table_management.register_table_input_nodes_concat_with_tf(
                self._tf_df, overwrite=True
            )

            # Swap linker reference under lock (held for microseconds only)
            with self._linker_swap_lock:
                self._linker = linker_new
                self._splink_con = splink_con_new
                self._db_api = db_api_new

            log.info(
                "EM training COMPLETE: Linker updated with trained parameters. "
                "This is FINAL STATE (not transient) - model will now use trained parameters for scoring."
            )

        except Exception as e:  # pylint: disable=broad-exception-caught  # Splink may raise undocumented exception types
            # Training failure: silently ignore, cold-start defaults remain active
            log.warning(
                "EM training FAILED or INCOMPLETE: %s. Model will continue using cold-start parameters. "
                "This is FINAL STATE (not transient) - training will be retried if resolve() is called again.",
                e,
            )

    def _apply_cold_start_params(self) -> None:
        """
        Apply cold-start m/u probability defaults to Splink linker.

        Reads splink.cold_start.comparisons from config and sets m/u probabilities
        on each comparison level in the linker's settings object.

        If cold_start section is absent, uses Splink's built-in defaults.

        Skips null levels (Splink's internal null-value handling level).

        INITIALIZATION STATE: Linker starts using these cold-start defaults for scoring.
        Once EM training completes, these are replaced with trained parameters.
        """
        # Check if cold_start config exists
        cold_start_cfg = self._config.get("splink", {}).get("cold_start", {})
        if not cold_start_cfg:
            log.info(
                "Linker initializing: No cold_start config found, using Splink defaults"
            )
            return

        comparisons_cfg = cold_start_cfg.get("comparisons", {})
        if not comparisons_cfg:
            log.info(
                "Linker initializing: No comparisons config in cold_start, using Splink defaults"
            )
            return

        log.info(
            "Linker initializing: Applying cold-start parameters from config. "
            "These are INITIAL STATE defaults - will be replaced by EM-trained parameters once training completes. "
            "Fields: %s",
            list(comparisons_cfg.keys()),
        )

        # Iterate through comparison levels and apply m/u probabilities
        # pylint: disable=protected-access  # Splink exposes no public API for settings introspection
        for _, comparison in enumerate(self._linker._settings_obj.comparisons):
            # Get the field name from the comparison
            field_name = None
            if hasattr(comparison, "output_column_name"):
                field_name = comparison.output_column_name
            elif hasattr(comparison, "_field_names") and comparison._field_names:
                field_name = comparison._field_names[0]
            # pylint: enable=protected-access

            if field_name not in comparisons_cfg:
                continue

            field_cfg = comparisons_cfg[field_name]

            log.trace(
                "_apply_cold_start_params: Field '%s' has %d comparison levels",
                field_name,
                len(comparison.comparison_levels),
            )

            # Collect non-null levels to properly map cold-start probabilities
            non_null_levels = [
                (i, level)
                for i, level in enumerate(comparison.comparison_levels)
                if not (hasattr(level, "is_null_level") and level.is_null_level)
            ]
            log.trace(
                "_apply_cold_start_params: Field '%s' has %d non-null levels: %s",
                field_name,
                len(non_null_levels),
                [i for i, _ in non_null_levels],
            )

            # Apply m-probabilities to non-null levels in order
            if "m_probabilities" in field_cfg:
                m_probs = field_cfg["m_probabilities"]
                for config_idx, m_prob in enumerate(m_probs):
                    if config_idx < len(non_null_levels):
                        actual_level_idx, level = non_null_levels[config_idx]
                        try:
                            level.m_probability = m_prob
                            log.trace(
                                "_apply_cold_start_params: Set %s (actual level %d) m_prob=%.4f",
                                field_name,
                                actual_level_idx,
                                m_prob,
                            )
                        except (AttributeError, ValueError) as e:
                            # If setting fails, skip this level gracefully
                            log.trace(
                                "_apply_cold_start_params: Failed to set m_prob for %s level %d: %s",
                                field_name,
                                actual_level_idx,
                                e,
                            )

            # Apply u-probabilities to non-null levels in order
            if "u_probabilities" in field_cfg:
                u_probs = field_cfg["u_probabilities"]
                for config_idx, u_prob in enumerate(u_probs):
                    if config_idx < len(non_null_levels):
                        actual_level_idx, level = non_null_levels[config_idx]
                        try:
                            level.u_probability = u_prob
                            log.trace(
                                "_apply_cold_start_params: Set %s (actual level %d) u_prob=%.4f",
                                field_name,
                                actual_level_idx,
                                u_prob,
                            )
                        except (AttributeError, ValueError) as e:
                            # If setting fails, skip this level gracefully
                            log.trace(
                                "_apply_cold_start_params: Failed to set u_prob for %s level %d: %s",
                                field_name,
                                actual_level_idx,
                                e,
                            )

    def _log_trained_parameters(self, linker: Linker) -> None:
        """
        Extract and log all trained parameters from a trained Splink linker.

        Displays:
        - Fellegi-Sunter prior (lambda): P(match) for any two random records
        - m-probabilities: likelihood of observing comparison level when records match
        - u-probabilities: likelihood of observing comparison level when records don't match
        - Which fields were fully trained vs partially/not trained

        This is called AFTER EM completes, providing visibility into what the model learned.
        """
        try:
            # Get the Fellegi-Sunter prior (lambda)
            prior = None
            # pylint: disable=protected-access  # Splink exposes no public API for settings introspection
            if hasattr(linker._settings_obj, "probability_two_random_records_match"):
                prior = linker._settings_obj.probability_two_random_records_match
                log.info(
                    "EM trained parameter: lambda (P(match)) = %.6f",
                    prior,
                )

            # Iterate through comparisons and extract trained m/u probabilities
            for comparison in linker._settings_obj.comparisons:
                # Get field name
                field_name = None
                if hasattr(comparison, "output_column_name"):
                    field_name = comparison.output_column_name
                elif hasattr(comparison, "_field_names") and comparison._field_names:
                    field_name = comparison._field_names[0]
                # pylint: enable=protected-access

                if not field_name:
                    continue

                log.info(
                    "EM trained parameters for field '%s':",
                    field_name,
                )

                # Collect non-null levels
                non_null_levels = [
                    (i, level)
                    for i, level in enumerate(comparison.comparison_levels)
                    if not (hasattr(level, "is_null_level") and level.is_null_level)
                ]

                # Log m and u probabilities for each level
                for config_idx, (_, level) in enumerate(non_null_levels):
                    m_prob = None
                    u_prob = None
                    trained_m = False
                    trained_u = False

                    # Extract m-probability
                    if (
                        hasattr(level, "m_probability")
                        and level.m_probability is not None
                    ):
                        m_prob = level.m_probability
                        # Check if it was trained (non-cold-start values have specific patterns)
                        # Cold-start values are typically set exactly; trained values may vary
                        trained_m = True

                    # Extract u-probability
                    if (
                        hasattr(level, "u_probability")
                        and level.u_probability is not None
                    ):
                        u_prob = level.u_probability
                        trained_u = True

                    # Log level details
                    level_desc = getattr(level, "label", f"Level {config_idx}")
                    m_status = "✓ trained" if trained_m else "✗ cold-start"
                    u_status = "✓ trained" if trained_u else "✗ cold-start"

                    log.info(
                        "  Level '%s': m_prob=%.6f (%s), u_prob=%.6f (%s)",
                        level_desc,
                        m_prob if m_prob is not None else 0.0,
                        m_status,
                        u_prob if u_prob is not None else 0.0,
                        u_status,
                    )

        except Exception as e:  # pylint: disable=broad-exception-caught  # Splink may raise undocumented exception types
            log.warning(
                "_log_trained_parameters: Could not extract trained parameters: %s",
                e,
            )
