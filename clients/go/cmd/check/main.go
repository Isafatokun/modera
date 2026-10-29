// Command check verifies the Go client against a local rules-only reference API.
package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"image"
	"image/png"
	"os"
	"time"

	modera "github.com/Isafatokun/modera/clients/go"
)

func check() error {
	client, err := modera.New(os.Getenv("MODERA_CHECK_BASE_URL"), os.Getenv("MODERA_API_KEY"), modera.Options{TotalTimeout: 5 * time.Second})
	if err != nil {
		return err
	}
	ctx := context.Background()
	blocked, err := client.ModerateText(ctx, "example prohibited phrase", "rules-only")
	if err != nil {
		return err
	}
	if blocked.Decision.Action != "block" || blocked.RulesVersion == nil || blocked.Fallback || len(blocked.Models) != 0 {
		return errors.New("rules block/provenance check failed")
	}
	clean, err := client.ModerateText(ctx, "Bonjour, merci pour ton aide.", "rules-only")
	if err != nil {
		return err
	}
	if clean.Decision.Action != "allow" || clean.RequestID == blocked.RequestID {
		return errors.New("rules allow check failed")
	}
	var photo bytes.Buffer
	if err = png.Encode(&photo, image.NewRGBA(image.Rect(0, 0, 2, 2))); err != nil {
		return err
	}
	result, err := client.ModerateImage(ctx, photo.Bytes(), "default")
	var api *modera.APIError
	if result != nil || !errors.As(err, &api) || api.Status != 501 || api.Code != "moderation_not_implemented" || api.Attempts != 1 {
		return errors.New("unconfigured image/error contract check failed")
	}
	result, err = client.ModerateImageURL(ctx, "https://unreachable.invalid/photo.png", "default")
	if result != nil || !errors.As(err, &api) || api.Code != "image_url_not_allowed" || api.Attempts != 1 {
		return errors.New("image URL rejection check failed")
	}
	bad, err := modera.New(os.Getenv("MODERA_CHECK_BASE_URL"), "incorrect-test-key-with-at-least-32-characters", modera.Options{})
	if err != nil {
		return err
	}
	result, err = bad.ModerateText(ctx, "hello", "rules-only")
	if result != nil || !errors.As(err, &api) || api.Status != 401 || api.Attempts != 1 {
		return errors.New("authentication check failed")
	}
	fmt.Println("Go client: rules block/allow, provenance, image upload, URL rejection and authentication passed")
	return nil
}
func main() {
	if err := check(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
