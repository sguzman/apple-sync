//go:build apple_sync_experimental && !plan9 && !solaris

package iclouddrive

import (
    "context"
    "errors"
    "testing"

    "github.com/rclone/rclone/fs"
)

func TestAppleSyncBatchRejectsUnsupportedCommands(t *testing.T) {
    f := &Fs{}
    _, err := f.Command(context.Background(), "not-apple-sync", []string{"safe"}, nil)
    if !errors.Is(err, fs.ErrorCommandNotFound) {
        t.Fatalf("unknown command must be rejected, got %v", err)
    }
}

func TestAppleSyncBatchRejectsInvalidRequestsBeforeNetwork(t *testing.T) {
    f := &Fs{}
    valid := "FOLDER::com.apple.CloudDocs::valid"
    cases := []struct {
        name string
        args []string
        opts map[string]string
    }{
        {"empty", nil, nil},
        {"wrong type", []string{"FILE::zone::id"}, nil},
        {"bad path", []string{"FOLDER::zone::bad/name"}, nil},
        {"duplicate", []string{valid, valid}, nil},
        {"etag", []string{valid + "#tag"}, nil},
        {"opts", []string{valid}, map[string]string{"danger": "true"}},
        {"too many", []string{valid, valid + "1", valid + "2", valid + "3", valid + "4", valid + "5", valid + "6", valid + "7", valid + "8"}, nil},
    }
    for _, tc := range cases {
        t.Run(tc.name, func(t *testing.T) {
            _, err := f.Command(context.Background(), appleSyncBatchCommand, tc.args, tc.opts)
            if err == nil {
                t.Fatal("invalid request reached uninitialized network transport")
            }
        })
    }
}

func TestAppleSyncBatchRejectsNonRootFs(t *testing.T) {
    f := &Fs{root: "different/remote"}
    _, err := f.Command(context.Background(), appleSyncBatchCommand,
        []string{"FOLDER::com.apple.CloudDocs::valid"}, nil)
    if err == nil {
        t.Fatal("a subdirectory fs must not access arbitrary cached Drive IDs")
    }
}
