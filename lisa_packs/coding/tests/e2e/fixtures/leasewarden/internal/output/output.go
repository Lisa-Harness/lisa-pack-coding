// Package output formats flat JSON resource rows for people or shell pipelines.
package output

import (
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
	"text/tabwriter"
)

// Write renders a decoded API response. Unknown formats are errors.
func Write(w io.Writer, format string, value any) error {
	switch format {
	case "json":
		encoder := json.NewEncoder(w)
		encoder.SetIndent("", "  ")
		return encoder.Encode(value)
	case "table":
		return writeTable(w, value)
	default:
		return fmt.Errorf("unknown output format %q", format)
	}
}

func writeTable(w io.Writer, value any) error {
	var rows []map[string]any
	switch item := value.(type) {
	case nil:
		return nil
	case map[string]any:
		rows = []map[string]any{item}
	case []any:
		for _, row := range item {
			record, ok := row.(map[string]any)
			if !ok {
				return fmt.Errorf("table expects object rows")
			}
			rows = append(rows, record)
		}
	default:
		return fmt.Errorf("table expects an object or object array")
	}
	if len(rows) == 0 {
		_, err := fmt.Fprintln(w, "(empty)")
		return err
	}
	keys := make(map[string]bool)
	for _, row := range rows {
		for key := range row {
			keys[key] = true
		}
	}
	columns := make([]string, 0, len(keys))
	for _, key := range []string{"mac", "ip", "hostname", "expires", "client_id", "addresses"} {
		if keys[key] {
			columns = append(columns, key)
			delete(keys, key)
		}
	}
	rest := make([]string, 0, len(keys))
	for key := range keys {
		rest = append(rest, key)
	}
	sort.Strings(rest)
	columns = append(columns, rest...)
	table := tabwriter.NewWriter(w, 0, 4, 2, ' ', 0)
	header := make([]string, len(columns))
	for i, column := range columns {
		header[i] = strings.ToUpper(column)
	}
	if _, err := fmt.Fprintln(table, strings.Join(header, "\t")); err != nil {
		return err
	}
	for _, row := range rows {
		cells := make([]string, len(columns))
		for i, column := range columns {
			value := row[column]
			if value == nil {
				continue
			}
			if array, ok := value.([]any); ok {
				parts := make([]string, len(array))
				for j, element := range array {
					parts[j] = safeCell(fmt.Sprint(element))
				}
				cells[i] = strings.Join(parts, ",")
			} else {
				cells[i] = safeCell(fmt.Sprint(value))
			}
		}
		if _, err := fmt.Fprintln(table, strings.Join(cells, "\t")); err != nil {
			return err
		}
	}
	return table.Flush()
}

func safeCell(value string) string {
	return strings.Map(func(r rune) rune {
		if r < 32 || r == 127 {
			return ' '
		}
		return r
	}, value)
}
