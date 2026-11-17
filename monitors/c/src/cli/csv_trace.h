#ifndef R2U2_CLI_CSV_TRACE_H
#define R2U2_CLI_CSV_TRACE_H

#include "internals/errors.h"
#include "memory/monitor.h"
#include <stdint.h>
#include <stdbool.h>

typedef struct {
  const uint8_t* name;
  size_t name_len;
  uint8_t type;
} r2u2_csv_input_column_t;

typedef struct {
  FILE* input_file;
  char in_buf[BUFSIZ];
  // Zero means the trace had no header; preserve legacy headerless parsing.
  size_t expected_columns;
  bool has_input_contract;
  bool first_record_processed;
  r2u2_csv_input_column_t input_columns[R2U2_MAX_SIGNALS];
} r2u2_csv_reader_t;

/// @brief Parse an optional versioned input contract from a complete binary.
/// Legacy binary files contain no contract and retain existing CSV behavior.
r2u2_status_t r2u2_csv_configure_contract(r2u2_csv_reader_t* reader,
    const uint8_t* spec, size_t spec_len);

/// @brief      Sets the R2U2 monitor's signal_vector (or atomic_vector if booleanizer is not enabled)
///             based on next line in CSV
/// @param[in]  csv_reader  Pointer to r2u2_csv_reader_t
/// @param[in]  monitor  Pointer to (configured) R2U2 monitor
/// @return     r2u2_status_t
r2u2_status_t r2u2_csv_load_next_signals(r2u2_csv_reader_t* csv_reader, r2u2_monitor_t* monitor);

#endif /* R2U2_CLI_CSV_TRACE_H */
