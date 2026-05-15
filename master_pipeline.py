import logging
import sys
import time

from conversation_rollup import run_rollup
from database_manager import close_db_pool
from graph_extractor import run_search

NIGHTLY_CLIENTS = ["mahle", "nidec", "valeo"]


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main():
    configure_logging()
    logger = logging.getLogger("master_pipeline")

    extraction_failed = False
    rollup_failed = False
    pipeline_start = time.time()

    try:
        logger.info("Nightly ETL pipeline started.")
        logger.info("Phase 1 extraction starting for clients: %s", ", ".join(NIGHTLY_CLIENTS))

        phase_one_start = time.time()
        for client in NIGHTLY_CLIENTS:
            client_start = time.time()
            logger.info("Phase 1 extraction started for client '%s'.", client)
            try:
                run_search(custom_domain=client)
            except Exception:
                extraction_failed = True
                logger.exception("Phase 1 extraction crashed for client '%s'.", client)
            else:
                client_duration = time.time() - client_start
                logger.info(
                    "Phase 1 extraction completed for client '%s' in %.2f seconds.",
                    client,
                    client_duration,
                )

        phase_one_duration = time.time() - phase_one_start
        logger.info("Phase 1 extraction finished in %.2f seconds.", phase_one_duration)

        phase_two_start = time.time()
        logger.info("Phase 2 conversation rollup starting.")
        try:
            run_rollup()
        except Exception:
            rollup_failed = True
            logger.exception("Phase 2 conversation rollup crashed.")
        else:
            phase_two_duration = time.time() - phase_two_start
            logger.info(
                "Phase 2 conversation rollup completed in %.2f seconds.",
                phase_two_duration,
            )

        total_duration = time.time() - pipeline_start
        if extraction_failed or rollup_failed:
            logger.error(
                "Nightly ETL pipeline finished with failures in %.2f seconds. "
                "extraction_failed=%s, rollup_failed=%s",
                total_duration,
                extraction_failed,
                rollup_failed,
            )
            return 1

        logger.info("Nightly ETL pipeline completed successfully in %.2f seconds.", total_duration)
        return 0
    except Exception:
        logger.exception("Nightly ETL pipeline crashed with an unexpected fatal error.")
        return 1
    finally:
        try:
            close_db_pool()
        except Exception:
            logger.exception("Failed to close the database connection pool cleanly.")
        else:
            logger.info("Database connection pool closed.")


if __name__ == "__main__":
    raise SystemExit(main())
