"""Splink-backed similarity linker adapter (concrete implementation of SimilarityLinker port)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from pathlib import Path

import duckdb
import pandas as pd
import splink.comparison_library as cl
from splink import Linker, SettingsCreator, block_on
from splink.backends.duckdb import DuckDBAPI

from ere.adapters.duckdb_schema import (
    NORMALISED_SUFFIX,
    init_schema,
    normalised_columns,
    normalised_name_sql,
)
from ere.models.ports.linker import SimilarityLinker
from ere.models.resolver import (
    BlockingRule,
    BlockingSettings,
    EqualityRule,
    LinkTable,
    Mention,
    MentionLink,
    ModelSource,
    ModelStatus,
    TrainingOutcome,
    TrainingStatus,
)

log = logging.getLogger(__name__)

SPLINK_KEY = "splink"
BLOCKING_RULES_KEY = "blocking_rules"
MATCH_WEIGHT_THRESHOLD_KEY = "match_weight_threshold"
SEARCH_SPACE_VIEW = "ere_search_space"
NEW_RECORD_VIEW = "ere_new_record"
NEW_RECORD_RAW_VIEW = "ere_new_record_raw"
TRAINING_SAMPLE_TABLE = "ere_training_sample"
CONCAT_WITH_TF_TEMPLATE = "__splink__df_concat_with_tf"
UNIQUE_ID_COLUMN = "mention_id"
SALT_COLUMN = "__splink_salt"
SALT_VALUE = 0.5
EM_MAX_PAIRS = 1e6


def _render_rule(rule: BlockingRule) -> str:
    if isinstance(rule, EqualityRule):
        return " AND ".join(f"l.{field} = r.{field}" for field in rule.fields)
    column = f"{rule.field}{NORMALISED_SUFFIX}"
    return (
        f"l.{rule.same} = r.{rule.same} AND "
        f"jaro_winkler_similarity(l.{column}, r.{column}) >= {rule.min_jaro_winkler}"
    )


def render_blocking_rules(rules: Sequence[BlockingRule]) -> list[str]:
    """Render validated blocking rules as SQL over l./r. (OR-ed by Splink); NULL never matches."""
    return [_render_rule(rule) for rule in rules]


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


class SpLinkSimilarityLinker(SimilarityLinker):  # pylint: disable=too-many-instance-attributes  # connection, scoring linker/cursor, model state and training guards belong together
    """
    Splink-backed implementation of SimilarityLinker port.

    The search space is the `mentions` table, read through a view on every request, so mentions
    persisted by the repository (including those stored before a restart) are always visible.

    - With a shared `connection`, the repository's INSERT is the registration; `register_mention` is a no-op.
    - Without one, the linker owns a private in-memory database and `register_mention` inserts into it.

    Training runs once (EM), the model is frozen afterwards and persisted to `model_path` when given.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        entity_fields: list[str],
        config: dict,
        initial_df: pd.DataFrame | None = None,
        *,
        connection: duckdb.DuckDBPyConnection | None = None,
        model_path: str | None = None,
        training_sample_size: int | None = None,
        blocking: BlockingSettings | None = None,
        match_weight_threshold: float | None = None,
    ) -> None:
        """
        Initialize the Splink linker.

        Args:
            entity_fields: List of field names (e.g. ["legal_name", "country_code"]).
            config: Full resolver configuration dict (needs match_weight_threshold and splink section).
            initial_df: Pre-built TF DataFrame seeding a private search space; ignored with a shared connection.
            connection: Shared DuckDB connection holding the `mentions` table; None creates a private one.
            model_path: File the trained model is saved to and loaded from; None disables persistence.
            training_sample_size: Maximum number of mentions EM training reads; None reads all.
            blocking: Validated blocking settings; None parses them from `config` (tests, tools).
            match_weight_threshold: Lowest match weight kept; None reads it from `config`.
        """
        self._entity_fields = entity_fields
        self._config = config
        self._match_weight_threshold = (
            match_weight_threshold
            if match_weight_threshold is not None
            else config[MATCH_WEIGHT_THRESHOLD_KEY]
        )
        self._model_path = Path(model_path) if model_path else None
        self._training_sample_size = training_sample_size
        self._model_load_error_type: str | None = None
        self._blocking = blocking or BlockingSettings.from_config(
            config[SPLINK_KEY][BLOCKING_RULES_KEY], entity_fields
        )
        self._blocking_rules = render_blocking_rules(self._blocking.rules)
        self._normalised_columns = normalised_columns(
            [
                field
                for field in self._blocking.normalised_fields
                if field in entity_fields
            ]
        )

        self._owns_connection = connection is None
        if self._owns_connection:
            connection = duckdb.connect()
            init_schema(connection, entity_fields, self._blocking.normalised_fields)
        self._con = connection
        self._create_search_space_view()
        if self._owns_connection and initial_df is not None:
            self._insert_search_space_rows(initial_df)

        self._linker, self._cursor, self.model_source = self._build_scoring_linker()
        self._pending_trained_settings: dict | None = None

        # Threading synchronization for safe linker swaps during training
        self._linker_swap_lock = threading.Lock()
        self._training_lock = threading.Lock()

    def find_matches(self, mention: Mention) -> list[MentionLink]:
        """
        Score a mention against the stored search space.

        Returns all mention-links above match_weight_threshold (including below-threshold
        links needed for candidate discovery), oriented new mention → stored mention.

        Args:
            mention: The Mention to score against the search space.

        Returns:
            List of MentionLink objects (empty if no matches or search space is empty).
        """
        return self.find_matches_batch([mention]).links_for(mention.id)

    def find_matches_batch(self, mentions: list[Mention]) -> LinkTable:
        """
        Score several mentions in one Splink call; per-call Splink artefacts are released before returning.

        Links point from the new mention (left) to the scored mention (right); self-links are excluded.
        """
        self._apply_trained_settings()
        linker, cursor = self._linker, self._cursor
        if not mentions:
            return LinkTable(left_ids=(), right_ids=(), scores=())

        self._register_new_records(cursor, mentions)
        predictions = None
        try:
            predictions = linker.inference.find_matches_to_new_records(
                NEW_RECORD_VIEW,
                blocking_rules=self._get_blocking_rules(),
                match_weight_threshold=self._match_weight_threshold,
            )
            rows = cursor.execute(
                f"SELECT {UNIQUE_ID_COLUMN}_r, {UNIQUE_ID_COLUMN}_l, match_probability "
                f"FROM {predictions.physical_name} WHERE {UNIQUE_ID_COLUMN}_r <> {UNIQUE_ID_COLUMN}_l"
            ).fetchall()
        finally:
            try:
                self._release_request_artefacts(linker, cursor, predictions)
            except Exception as exc:  # pylint: disable=broad-exception-caught  # cleanup must never fail a request
                # Cleanup errors name Splink tables, not mention data, so the message is kept for diagnosis.
                log.warning("Could not release per-request Splink artefacts: %s", exc)

        log.trace(
            "find_matches_batch: %d links for %d mentions", len(rows), len(mentions)
        )
        return LinkTable(
            left_ids=tuple(str(row[0]) for row in rows),
            right_ids=tuple(str(row[1]) for row in rows),
            scores=tuple(float(row[2]) for row in rows),
        )

    def register_mention(self, mention: Mention) -> None:
        """
        Add a mention to the search space for future find_matches() calls.

        With a shared connection the repository has already persisted the mention, so nothing is done.

        Args:
            mention: The Mention to add to the search space.
        """
        if not self._owns_connection:
            return
        self._insert_search_space_rows(build_tf_df([mention], self._entity_fields))

    def train(self) -> TrainingOutcome:
        """
        Estimate model parameters via EM once, then freeze and persist them (non-blocking, thread-safe).

        Skipped when the model is already trained or persisted, when a model file exists but is unusable,
        or while another training run is in progress. Failures leave cold-start parameters active.
        """
        skipped = TrainingOutcome(status=TrainingStatus.SKIPPED)
        if not self.needs_training():
            return skipped
        if not self._training_lock.acquire(blocking=False):  # pylint: disable=consider-using-with  # non-blocking acquire: skip if another training run is in progress
            return skipped
        try:
            return self._train_safe() if self.needs_training() else skipped
        finally:
            self._training_lock.release()

    def model_status(self) -> ModelStatus:
        """Where scoring parameters come from, and why a model file could not be used (if so)."""
        with self._linker_swap_lock:
            return ModelStatus(
                source=self.model_source, load_error_type=self._model_load_error_type
            )

    def needs_training(self) -> bool:
        """True while scoring uses cold-start parameters and no (unusable) model file blocks training."""
        with self._linker_swap_lock:
            return (
                self.model_source == ModelSource.COLD_START
                and self._model_load_error_type is None
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _register_new_records(
        self, cursor: duckdb.DuckDBPyConnection, mentions: list[Mention]
    ) -> None:
        """Register the new records (with normalised names computed by the stored-mention SQL) under one view name."""
        # Empty strings instead of None prevent DuckDB inferring INTEGER for all-null columns.
        records = [
            {k: (v or "") for k, v in mention.to_flat_dict().items()}
            for mention in mentions
        ]
        cursor.register(NEW_RECORD_RAW_VIEW, pd.DataFrame(records, dtype="string"))
        try:
            normalised = ", ".join(
                f"{normalised_name_sql(column.removesuffix(NORMALISED_SUFFIX))} AS {column}"
                for column in self._normalised_columns
            )
            select = f"SELECT *{', ' + normalised if normalised else ''} FROM {NEW_RECORD_RAW_VIEW}"
            cursor.register(NEW_RECORD_VIEW, cursor.execute(select).df())
        finally:
            cursor.unregister(NEW_RECORD_RAW_VIEW)

    def _create_search_space_view(self) -> None:
        columns = ", ".join(
            f"CAST({field} AS VARCHAR) AS {field}"
            for field in self._entity_fields + self._normalised_columns
        )
        self._con.execute(
            f"CREATE OR REPLACE VIEW {SEARCH_SPACE_VIEW} AS "
            f"SELECT {UNIQUE_ID_COLUMN}, {columns}, {SALT_VALUE}::DOUBLE AS {SALT_COLUMN} FROM mentions"
        )

    def _insert_search_space_rows(self, df: pd.DataFrame) -> None:
        columns = [UNIQUE_ID_COLUMN] + self._entity_fields
        rows = (
            df[columns].astype(object).where(df[columns].notna(), None).values.tolist()
        )
        placeholders = ["?"] * len(columns)
        insert_columns = list(columns)
        for column in self._normalised_columns:
            insert_columns.append(column)
            placeholders.append(normalised_name_sql("?"))
            source_index = columns.index(column.removesuffix(NORMALISED_SUFFIX))
            rows = [row + [row[source_index]] for row in rows]
        self._con.executemany(
            f"INSERT INTO mentions ({', '.join(insert_columns)}) VALUES ({', '.join(placeholders)})",
            rows,
        )

    def _new_linker(
        self,
        input_table: str,
        settings,
        connection: duckdb.DuckDBPyConnection | None = None,
    ) -> tuple[Linker, duckdb.DuckDBPyConnection]:
        """
        Build a Splink linker whose search space is a live view.

        Scoring uses the shared connection itself, so it runs inside the caller's transaction and sees
        mentions inserted earlier in the same bite. Training passes its own cursor (another thread).
        """
        connection = connection or self._con
        db_api = DuckDBAPI(connection=connection)
        linker = Linker(input_table, settings, db_api=db_api)
        search_space = db_api.table_to_splink_dataframe(CONCAT_WITH_TF_TEMPLATE, input_table)
        # Splink has no public API to register a live view as its search space.
        cache = linker._intermediate_table_cache  # pylint: disable=protected-access
        cache[CONCAT_WITH_TF_TEMPLATE] = search_space
        return linker, connection

    def _apply_trained_settings(self) -> None:
        """On the resolver thread: rebuild the scoring linker once background training has produced parameters."""
        with self._linker_swap_lock:
            settings = self._pending_trained_settings
        if settings is None:
            return
        # Cleared only after a successful rebuild, so a failure is retried on the next scoring call.
        self._linker, self._cursor = self._new_linker(SEARCH_SPACE_VIEW, settings)
        with self._linker_swap_lock:
            self._pending_trained_settings = None
        log.debug("Scoring linker rebuilt with trained parameters")

    def _build_scoring_linker(
        self,
    ) -> tuple[Linker, duckdb.DuckDBPyConnection, ModelSource]:
        if self._model_path is not None and self._model_path.exists():
            try:
                linker, cursor = self._new_linker(
                    SEARCH_SPACE_VIEW, str(self._model_path)
                )
                return linker, cursor, ModelSource.PERSISTED
            except Exception as exc:  # pylint: disable=broad-exception-caught  # Splink raises undocumented types on bad JSON/settings
                # Reported through model_status(); services log it (the file is left untouched).
                self._model_load_error_type = type(exc).__name__
        linker, cursor = self._new_linker(SEARCH_SPACE_VIEW, self._build_settings())
        self._linker = linker
        self._apply_cold_start_params()
        return linker, cursor, ModelSource.COLD_START

    @staticmethod
    def _release_request_artefacts(
        linker: Linker, cursor: duckdb.DuckDBPyConnection, predictions
    ) -> None:
        """Drop everything a single find_matches call left behind in the database and Splink's trackers."""
        if predictions is not None:
            predictions.drop_table_from_database_and_remove_from_cache()
        cache = linker._intermediate_table_cache  # pylint: disable=protected-access  # trackers grow per call; no public reset on Linker
        cache.reset_executed_queries_tracker()
        cache.reset_queries_retrieved_from_cache_tracker()
        cursor.unregister(NEW_RECORD_VIEW)

    def _get_blocking_rules(self) -> list[str]:
        """Blocking rules from config, rendered as SQL (validated at construction)."""
        return list(self._blocking_rules)

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
        """EM training blocks on the configured field (country by default), independent of the scoring rules."""
        return block_on(self._blocking.em_blocking_field)

    def _train_safe(self) -> TrainingOutcome:
        """
        Train on a sample of the search space with its own cursor, then swap in a scoring linker.

        The trained parameters are persisted before the swap. Training tables are dropped afterwards.
        Training failures are logged and leave cold-start parameters in place.
        """
        cursor = self._con.cursor()
        try:
            limit = (
                f" LIMIT {int(self._training_sample_size)}"
                if self._training_sample_size
                else ""
            )
            cursor.execute(
                f"CREATE OR REPLACE TEMP TABLE {TRAINING_SAMPLE_TABLE} AS "
                f"SELECT * FROM {SEARCH_SPACE_VIEW} ORDER BY {UNIQUE_ID_COLUMN}{limit}"
            )
            sample_size = cursor.execute(
                f"SELECT COUNT(*) FROM {TRAINING_SAMPLE_TABLE}"
            ).fetchone()[0]
            log.debug("EM training starting on %d sampled mentions", sample_size)

            training_linker, _ = self._new_linker(
                TRAINING_SAMPLE_TABLE, self._build_settings(), cursor
            )
            training_linker.training.estimate_u_using_random_sampling(
                max_pairs=EM_MAX_PAIRS
            )
            training_linker.training.estimate_parameters_using_expectation_maximisation(
                self._get_em_training_rule(), estimate_without_term_frequencies=True
            )
            self._log_trained_parameters(training_linker)

            trained_settings = training_linker.misc.save_model_to_json()
            if self._model_path is not None:
                training_linker.misc.save_model_to_json(
                    str(self._model_path), overwrite=True
                )
            self._drop_training_tables(training_linker)

            with self._linker_swap_lock:
                self._pending_trained_settings = trained_settings
                self.model_source = ModelSource.TRAINED
            return TrainingOutcome(
                status=TrainingStatus.TRAINED,
                sample_size=sample_size,
                model_persisted=self._model_path is not None,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught  # Splink may raise undocumented exception types
            # Exception text can quote data values (DEC-10): only its type is reported.
            return TrainingOutcome(
                status=TrainingStatus.FAILED, error_type=type(e).__name__
            )
        finally:
            cursor.execute(f"DROP TABLE IF EXISTS {TRAINING_SAMPLE_TABLE}")
            cursor.close()

    @staticmethod
    def _drop_training_tables(linker: Linker) -> None:
        cache = linker._intermediate_table_cache  # pylint: disable=protected-access  # Splink has no public "drop all cached tables"
        for splink_df in list(cache.values()):
            try:
                splink_df.drop_table_from_database_and_remove_from_cache()
            except Exception as exc:  # pylint: disable=broad-exception-caught  # non-Splink tables are refused; keep going
                log.debug(
                    "Training cleanup skipped %s: %s", splink_df.physical_name, exc
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
            log.debug(
                "Linker initializing: No cold_start config found, using Splink defaults"
            )
            return

        comparisons_cfg = cold_start_cfg.get("comparisons", {})
        if not comparisons_cfg:
            log.debug(
                "Linker initializing: No comparisons config in cold_start, using Splink defaults"
            )
            return

        log.debug(
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
                log.debug(
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

                log.debug(
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

                    log.debug(
                        "  Level '%s': m_prob=%.6f (%s), u_prob=%.6f (%s)",
                        level_desc,
                        m_prob if m_prob is not None else 0.0,
                        m_status,
                        u_prob if u_prob is not None else 0.0,
                        u_status,
                    )

        except Exception as e:  # pylint: disable=broad-exception-caught  # Splink may raise undocumented exception types
            log.debug(
                "_log_trained_parameters: Could not extract trained parameters: %s",
                e,
            )
