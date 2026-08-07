use serde::Serialize;
use std::fmt;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct DesktopError {
    pub code: String,
    pub message: String,
}

impl DesktopError {
    pub(crate) fn new(code: &'static str, message: &'static str) -> Self {
        Self {
            code: code.to_owned(),
            message: message.to_owned(),
        }
    }

    pub(crate) fn invalid_request(message: &'static str) -> Self {
        Self::new("INVALID_REQUEST", message)
    }

    pub(crate) fn from_bridge(code: &str) -> Self {
        match code {
            "INVALID_REQUEST" => Self::new("INVALID_REQUEST", "The request was invalid."),
            "REQUEST_TOO_LARGE" => {
                Self::new("REQUEST_TOO_LARGE", "The request exceeds the allowed size.")
            }
            "INVALID_JSON" => Self::new("INVALID_JSON", "The bridge rejected malformed JSON."),
            "UNKNOWN_OPERATION" => Self::new(
                "UNKNOWN_OPERATION",
                "The requested operation is not available.",
            ),
            "INVALID_PARAMS" => {
                Self::new("INVALID_PARAMS", "The operation parameters were invalid.")
            }
            "NOT_FOUND" => Self::new("NOT_FOUND", "The requested local record was not found."),
            "VERSION_CONFLICT" => Self::new(
                "VERSION_CONFLICT",
                "The record changed. Refresh before trying again.",
            ),
            "STALE_PURGE_PLAN" => Self::new(
                "STALE_PURGE_PLAN",
                "The forget preview changed. Review the updated impact before continuing.",
            ),
            "PURGE_CLOSURE_UNVERIFIABLE" => Self::new(
                "PURGE_CLOSURE_UNVERIFIABLE",
                "A damaged local provenance record prevents a safe forget operation.",
            ),
            "BUSY" => Self::new(
                "BUSY",
                "OpenChronicle is busy with another local operation. Try again shortly.",
            ),
            "INTERNAL_ERROR" => Self::new(
                "INTERNAL_ERROR",
                "OpenChronicle could not complete the local operation.",
            ),
            _ => Self::new(
                "BRIDGE_PROTOCOL_ERROR",
                "The desktop bridge returned an unsupported error.",
            ),
        }
    }
}

impl fmt::Display for DesktopError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for DesktopError {}

#[cfg(test)]
mod tests {
    use super::DesktopError;

    #[test]
    fn backend_messages_are_not_forwarded() {
        let error = DesktopError::from_bridge("VERSION_CONFLICT");
        assert_eq!(error.code, "VERSION_CONFLICT");
        assert!(!error.message.contains("backend"));

        let unknown = DesktopError::from_bridge("SOMETHING_NEW");
        assert_eq!(unknown.code, "BRIDGE_PROTOCOL_ERROR");

        let unverifiable = DesktopError::from_bridge("PURGE_CLOSURE_UNVERIFIABLE");
        assert_eq!(unverifiable.code, "PURGE_CLOSURE_UNVERIFIABLE");
        assert!(unverifiable.message.contains("safe forget"));
    }
}
