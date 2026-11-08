//go:build apple_sync_experimental && !plan9 && !solaris

// Read-only experimental command for a locally built rclone test binary.
// Copy into backend/iclouddrive of a pinned rclone source checkout and build
// with -tags apple_sync_experimental. Stock rclone remains unchanged.
package iclouddrive

import (
    "context"
    "errors"
    "net/http"
    "regexp"

    "github.com/rclone/rclone/backend/iclouddrive/api"
    "github.com/rclone/rclone/fs"
)

const appleSyncBatchCommand = "apple-sync-read-batch"
const appleSyncBatchMaxFolders = 8

var appleSyncDriveID = regexp.MustCompile("^(FOLDER|APP_CONTAINER|APP_LIBRARY)::[^:#/\\s]+::[^#/\\s]+$")

// Command implements rclone's optional fs.Commander interface. Features.Fill
// discovers this method automatically; no existing rclone source is edited.
// Only a strict, read-only command is exposed in the tagged build.
func (f *Fs) Command(ctx context.Context, name string, arg []string, opt map[string]string) (any, error) {
    if name != appleSyncBatchCommand {
        return nil, fs.ErrorCommandNotFound
    }
    if f.root != "" || len(opt) != 0 || len(arg) < 1 || len(arg) > appleSyncBatchMaxFolders {
        return nil, errors.New("APPLE_SYNC_BATCH_INVALID_REQUEST")
    }
    seen := make(map[string]bool, len(arg))
    for _, id := range arg {
        if !appleSyncDriveID.MatchString(id) || seen[id] {
            return nil, errors.New("APPLE_SYNC_BATCH_INVALID_IDS")
        }
        seen[id] = true
    }
    var items []*api.DriveItem
    var resp *http.Response
    var err error
    err = f.pacer.Call(func() (bool, error) {
        items, resp, err = f.service.GetItemsByDriveID(ctx, arg, true)
        return shouldRetry(ctx, resp, err)
    })
    if err != nil {
        // Raw Apple error strings may contain sensitive identifiers.
        return nil, errors.New("APPLE_SYNC_BATCH_REMOTE_FAILURE")
    }
    if len(items) != len(arg) {
        return nil, errors.New("APPLE_SYNC_BATCH_CARDINALITY")
    }
    results := make(map[string]map[string]any, len(arg))
    for _, item := range items {
        if item == nil || !seen[item.Drivewsid] || results[item.Drivewsid] != nil ||
            item.Status != "OK" || !item.IsFolder() || item.Items == nil ||
            item.DirectChildrenCount != int64(len(item.Items)) {
            return nil, errors.New("APPLE_SYNC_BATCH_UNVERIFIED_FOLDER")
        }
        children := make([]map[string]any, 0, len(item.Items))
        childIDs := make(map[string]bool, len(item.Items))
        for _, child := range item.Items {
            if child == nil || child.Drivewsid == "" || childIDs[child.Drivewsid] ||
                child.Name == "" {
                return nil, errors.New("APPLE_SYNC_BATCH_INVALID_CHILD")
            }
            childIDs[child.Drivewsid] = true
            if child.Type != "FILE" && !child.IsFolder() {
                return nil, errors.New("APPLE_SYNC_BATCH_UNSUPPORTED_TYPE")
            }
            children = append(children, map[string]any{
                "drivewsid":    child.Drivewsid,
                "type":         child.Type,
                "name":         f.opt.Enc.ToStandardName(child.Name),
                "extension":    f.opt.Enc.ToStandardName(child.Extension),
                "etag":         child.Etag,
                "dateModified": child.DateModified,
                "dateCreated":  child.DateCreated,
                "size":         child.Size,
                "assetQuota":   child.AssetQuota,
            })
        }
        results[item.Drivewsid] = map[string]any{
            "drivewsid":           item.Drivewsid,
            "type":                item.Type,
            "status":              item.Status,
            "directChildrenCount": item.DirectChildrenCount,
            "items":               children,
        }
    }
    // Output in request order only after verifying every target and child.
    ordered := make([]map[string]any, 0, len(arg))
    for _, id := range arg {
        result := results[id]
        if result == nil {
            return nil, errors.New("APPLE_SYNC_BATCH_UNMATCHED_ID")
        }
        ordered = append(ordered, result)
    }
    return ordered, nil
}
