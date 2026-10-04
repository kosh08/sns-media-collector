"""PyInstaller sidecar launcher for gallery-dl.

The bundled executable normally follows gallery-dl's interactive-input policy.
SNS Media Collector supplies the short-lived pixiv authorization code through a
pipe, so its explicitly requested OAuth mode needs a pipe-safe reader.
"""
import runpy
import sys


def enable_smc_post_previews() -> bool:
    """Add image-only preview metadata without changing media counts or numbering."""
    flag = "--smc-post-previews"
    if flag not in sys.argv:
        return False
    sys.argv.remove(flag)
    from gallery_dl.extractor.twitter import TwitterExtractor
    from post_previews import tweet_preview_metadata
    original = TwitterExtractor._transform_tweet
    if getattr(original, "smc_previews_enabled", False):
        return True

    def transform(extractor, tweet):
        metadata = original(extractor, tweet)
        metadata["smc_previews"] = tweet_preview_metadata(tweet)
        return metadata

    transform.smc_previews_enabled = True
    TwitterExtractor._transform_tweet = transform
    return True


SMC_PIXIV_STDIN_FLAG = "--smc-pixiv-stdin"


def enable_smc_pixiv_stdin() -> bool:
    """Enable pipe input only when the private launcher flag is present."""
    if SMC_PIXIV_STDIN_FLAG not in sys.argv:
        return False
    sys.argv.remove(SMC_PIXIV_STDIN_FLAG)

    from gallery_dl import exception
    from gallery_dl.extractor import oauth

    def _input_code(_extractor):
        value = sys.stdin.readline()
        if not value:
            raise exception.AbortExtraction("pixiv OAuth input pipe closed")
        return value.rpartition("=")[2].strip()

    oauth.OAuthPixiv._input_code = _input_code
    return True

if __name__ == "__main__":
    enable_smc_post_previews()
    enable_smc_pixiv_stdin()
    runpy.run_module("gallery_dl", run_name="__main__")
