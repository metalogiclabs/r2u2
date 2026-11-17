#include "internals/config.h"
#include "csv_trace.h"
#include "r2u2.h"
#include "internals/debug.h"
#include <ctype.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// C2PO's optional SIG1 footer is only for this CLI's trace/input boundary.
// The monitor instruction binary and embedded-target loaders stay unchanged.
r2u2_status_t r2u2_csv_configure_contract(r2u2_csv_reader_t* reader,
    const uint8_t* spec, size_t spec_len) {
  const size_t trailer_len = 12;
  const uint8_t magic[] = "R2U2SIG1";
  if (spec_len < 2 || spec[0] < 2 || (size_t)spec[0] > spec_len ||
      memchr(spec + 1, '\0', (size_t)spec[0] - 1) == NULL) {
    fprintf(stderr, "Invalid CSV: configuration header is malformed\n");
    return R2U2_ERR_OTHER;
  }

  // A legacy binary has neither the SIG1 header declaration nor footer.
  // If either marker survives alone, reject instead of silently downgrading
  // a damaged protected binary to legacy CSV semantics.
  const bool declared = strstr((const char*)(spec + 1), " SIG1") != NULL;
  const bool has_footer = spec_len >= trailer_len &&
      memcmp(spec + spec_len - sizeof(magic) + 1, magic, sizeof(magic) - 1) == 0;
  if (!declared && !has_footer) return R2U2_OK;

  if (!declared || !has_footer || spec_len < (size_t)spec[0] + trailer_len) {
    fprintf(stderr, "Invalid CSV: SIG1 header/footer contract mismatch\n");
    return R2U2_ERR_OTHER;
  }

  const uint8_t* footer = spec + spec_len - trailer_len;
  const size_t payload_len = ((size_t)footer[0] << 24) |
      ((size_t)footer[1] << 16) | ((size_t)footer[2] << 8) | footer[3];
  if (payload_len < 2 || payload_len > spec_len - trailer_len ||
      spec_len - trailer_len - payload_len < (size_t)spec[0] + 2) {
    fprintf(stderr, "Invalid CSV: SIG1 footer length is malformed\n");
    return R2U2_ERR_OTHER;
  }

  const uint8_t* cursor = footer - payload_len;
  const uint8_t* end = footer;
  if (cursor[-1] != '\0' || cursor[-2] != '\0') {
    fprintf(stderr, "Invalid CSV: SIG1 must follow legacy auxiliary terminators\n");
    return R2U2_ERR_OTHER;
  }
  size_t columns = ((size_t)cursor[0] << 8) | cursor[1];
  cursor += 2;
  if (columns == 0 || columns > R2U2_MAX_SIGNALS) {
    fprintf(stderr, "Invalid CSV: SIG1 signal count exceeds capacity\n");
    return R2U2_ERR_OTHER;
  }

  for (size_t i = 0; i < columns; i++) {
    if ((size_t)(end - cursor) < 3) {
      fprintf(stderr, "Invalid CSV: SIG1 column descriptor is truncated\n");
      return R2U2_ERR_OTHER;
    }
    uint8_t type = *cursor++;
    size_t name_len = ((size_t)cursor[0] << 8) | cursor[1];
    cursor += 2;
    if ((type != 'B' && type != 'I' && type != 'F') || name_len == 0 ||
        name_len > (size_t)(end - cursor) || memchr(cursor, '\0', name_len) != NULL) {
      fprintf(stderr, "Invalid CSV: SIG1 column name or type is invalid\n");
      return R2U2_ERR_OTHER;
    }
    reader->input_columns[i] = (r2u2_csv_input_column_t){cursor, name_len, type};
    cursor += name_len;
  }
  if (cursor != end) {
    fprintf(stderr, "Invalid CSV: trailing data inside SIG1 contract\n");
    return R2U2_ERR_OTHER;
  }

  reader->has_input_contract = true;
  reader->expected_columns = columns;
  return R2U2_OK;
}

static r2u2_status_t r2u2_csv_validate_header(const char* header,
    const r2u2_csv_reader_t* reader) {
  for (size_t i = 0; i < reader->expected_columns; i++) {
    const char* start = header;
    while (*start == ' ' || *start == '\t') start++;
    const char* end = start;
    while (*end && *end != ',' && *end != '\r' && *end != '\n') end++;
    const char* trimmed_end = end;
    while (trimmed_end > start && (trimmed_end[-1] == ' ' || trimmed_end[-1] == '\t'))
      trimmed_end--;
    const r2u2_csv_input_column_t* column = &reader->input_columns[i];
    if ((size_t)(trimmed_end - start) != column->name_len ||
        memcmp(start, column->name, column->name_len) != 0) {
      fprintf(stderr, "Invalid CSV: signal name/order disagrees with compiled contract at column %zu\n", i);
      return R2U2_ERR_OTHER;
    }
    header = *end == ',' ? end + 1 : end;
  }
  return R2U2_OK;
}

static r2u2_status_t r2u2_csv_load_contract_value(r2u2_csv_reader_t* reader,
    r2u2_monitor_t* monitor, size_t index, char* value) {
  while (isspace((unsigned char)*value)) value++;
  char* end = value + strlen(value);
  while (end > value && isspace((unsigned char)end[-1])) *--end = '\0';
  if (end == value) {
    fprintf(stderr, "Invalid CSV: empty signal value\n");
    return R2U2_ERR_OTHER;
  }

  switch (reader->input_columns[index].type) {
    case 'B':
      if (value[1] != '\0' || (value[0] != '0' && value[0] != '1')) break;
      r2u2_load_bool_signal(monitor, index, value[0] == '1');
      return R2U2_OK;
    case 'I': {
      errno = 0;
      char* parsed_end;
      long result = strtol(value, &parsed_end, 10);
      if (errno == ERANGE || *parsed_end != '\0' || parsed_end == value ||
          result < INT32_MIN || result > INT32_MAX) break;
      r2u2_load_int_signal(monitor, index, (r2u2_int)result);
      return R2U2_OK;
    }
    case 'F': {
      errno = 0;
      char* parsed_end;
      double result = strtod(value, &parsed_end);
      if (errno == ERANGE || *parsed_end != '\0' || parsed_end == value ||
          !isfinite(result)) break;
      r2u2_load_float_signal(monitor, index, (r2u2_float)result);
      return R2U2_OK;
    }
    default:
      break;
  }
  fprintf(stderr, "Invalid CSV: malformed or out-of-range signal at column %zu\n", index);
  return R2U2_ERR_OTHER;
}

static r2u2_status_t r2u2_csv_read_line(r2u2_csv_reader_t* csv_reader) {
  size_t used = 0;
  int next_char;
  // fgets/strlen cannot distinguish a physical embedded NUL from the string
  // terminator and can silently authorize a verdict for only a row prefix.
  // Read the physical record so all bytes are validated before tokenization.
  while ((next_char = fgetc(csv_reader->input_file)) != EOF && next_char != '\n') {
    if (next_char == '\0') {
      fprintf(stderr, "Invalid CSV: embedded NUL byte in record\n");
      return R2U2_ERR_OTHER;
    }
    if (used >= sizeof(csv_reader->in_buf) - 1) {
      fprintf(stderr, "Invalid CSV: record exceeds input buffer\n");
      return R2U2_ERR_OTHER;
    }
    csv_reader->in_buf[used++] = (char)next_char;
  }
  if (ferror(csv_reader->input_file)) {
    fprintf(stderr, "Invalid CSV: trace read failed\n");
    return R2U2_ERR_OTHER;
  }
  if (next_char == EOF && used == 0) return R2U2_END_OF_TRACE;
  csv_reader->in_buf[used] = '\0';
  // CR is only a line terminator, never a signal-field separator.
  char* carriage_return = strchr(csv_reader->in_buf, '\r');
  if (carriage_return != NULL && carriage_return[1] != '\n' && carriage_return[1] != '\0') {
    fprintf(stderr, "Invalid CSV: carriage return inside a signal\n");
    return R2U2_ERR_OTHER;
  }
  return R2U2_OK;
}

// Count comma-separated fields before strtok collapses empty fields. This
// also checks that the first and last field, and every field between them,
// are present. Do not modify the buffer: the monitor keeps pointers into it.
static r2u2_status_t r2u2_csv_count_fields(const char* row, uintptr_t* count) {
  uintptr_t fields = 0;
  bool has_value = false;

  for (const char* cursor = row; *cursor != '\0' && *cursor != '\r' && *cursor != '\n'; cursor++) {
    if (*cursor == ',') {
      if (!has_value) {
        fprintf(stderr, "Invalid CSV: empty column\n");
        return R2U2_ERR_OTHER;
      }
      fields++;
      has_value = false;
    } else if (!isspace((unsigned char)*cursor)) {
      has_value = true;
    }
  }
  if (!has_value) {
    fprintf(stderr, "Invalid CSV: empty column\n");
    return R2U2_ERR_OTHER;
  }
  *count = fields + 1;
  return R2U2_OK;
}

r2u2_status_t r2u2_csv_load_next_signals(r2u2_csv_reader_t* csv_reader, r2u2_monitor_t* monitor) {
  char* signal;
  uintptr_t i;
  uintptr_t columns;
  r2u2_status_t read_status;
  const uintptr_t capacity = monitor->bz_program_count.max_program_count == 0
      ? R2U2_MAX_ATOMICS : R2U2_MAX_SIGNALS;

  // Read in next line of trace to internal buffer for processing
  read_status = r2u2_csv_read_line(csv_reader);
  if (read_status != R2U2_OK) return read_status;

  // Skip header row, if it exists - note we only check for this on the first line
  if (!csv_reader->first_record_processed && csv_reader->in_buf[0] == '#') {
    const char* header = csv_reader->in_buf + 1;
    while (*header == ' ' || *header == '\t') header++;
    if (r2u2_csv_count_fields(header, &columns) != R2U2_OK) return R2U2_ERR_OTHER;
    if (columns > capacity) {
      fprintf(stderr, "Invalid CSV: header exceeds configured capacity\n");
      return R2U2_ERR_OTHER;
    }
    if (csv_reader->has_input_contract && columns != csv_reader->expected_columns) {
      fprintf(stderr, "Invalid CSV: trace header width disagrees with compiled contract\n");
      return R2U2_ERR_OTHER;
    }
    if (csv_reader->has_input_contract &&
        r2u2_csv_validate_header(header, csv_reader) != R2U2_OK) {
      return R2U2_ERR_OTHER;
    }
    csv_reader->expected_columns = columns;
    read_status = r2u2_csv_read_line(csv_reader);
    if (read_status != R2U2_OK) return read_status;
  } else if (csv_reader->has_input_contract && !csv_reader->first_record_processed) {
    fprintf(stderr, "Invalid CSV: SIG1 input contract requires a named CSV header\n");
    return R2U2_ERR_OTHER;
  }

  csv_reader->first_record_processed = true;

  if (csv_reader->expected_columns != 0) {
    if (r2u2_csv_count_fields(csv_reader->in_buf, &columns) != R2U2_OK)
      return R2U2_ERR_OTHER;
    if (columns != csv_reader->expected_columns) {
      fprintf(stderr, "Invalid CSV: row width does not match header\n");
      return R2U2_ERR_OTHER;
    }
  }
  
  for(i = 0, signal = strtok(csv_reader->in_buf, ",\r\n"); signal; i++, signal = strtok(NULL, ",\r\n")) {
    if (i >= capacity) {
      fprintf(stderr, "Invalid CSV: input signals exceed configured capacity\n");
      return R2U2_ERR_OTHER;
    }
    // Follow the pointer to the signal vector, then assign the ith element
    // Note this is a pointer into the r2u2_csv_reader_t in_buf which must
    // stay in place while the signal vector is live

    // If the first term starts with '@', read its timestamp and first signal.
    if (i == 0 && signal[0] == '@') {
      char* temp_var;
      char* timechar = strtok_r(signal+1, " ", &temp_var);
      char* time_end;
      unsigned long time_value;
      if (timechar == NULL || timechar[0] == '-') {
        fprintf(stderr, "Invalid CSV: malformed timestamp\n");
        return R2U2_ERR_OTHER;
      }
      errno = 0;
      time_value = strtoul(timechar, &time_end, 10);
      if (errno == ERANGE || time_end == timechar || *time_end != '\0' ||
          time_value > R2U2_TNT_TIME) {
        fprintf(stderr, "Invalid CSV: malformed or out-of-range timestamp\n");
        return R2U2_ERR_OTHER;
      }
      signal = strtok_r(NULL, " ", &temp_var);
      if (signal == NULL) {
        fprintf(stderr, "Invalid CSV: missing signal after timestamp\n");
        return R2U2_ERR_OTHER;
      }
      monitor->time_stamp = (r2u2_time) time_value;
      R2U2_DEBUG_PRINT("Event: %u\n", monitor->time_stamp);
    }
    if (csv_reader->has_input_contract) {
      if (r2u2_csv_load_contract_value(csv_reader, monitor, i, signal) != R2U2_OK)
        return R2U2_ERR_OTHER;
    } else {
      r2u2_load_string_signal(monitor, i, signal);
    }
  }

  return R2U2_OK;
}
