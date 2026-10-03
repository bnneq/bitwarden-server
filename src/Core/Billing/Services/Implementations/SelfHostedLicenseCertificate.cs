using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.RegularExpressions;
using Bit.Core.Settings;
using Bit.Core.Utilities;

namespace Bit.Core.Billing.Services;

/// <summary>Loads an explicitly pinned, public-only license verification certificate for this fork.</summary>
public static class SelfHostedLicenseCertificate
{
    public static X509Certificate2? Load(IGlobalSettings settings)
    {
        var path = settings.SelfHostedLicenseCertificatePath;
        var pin = settings.SelfHostedLicenseCertificateSha256;
        if (string.IsNullOrWhiteSpace(path) && string.IsNullOrWhiteSpace(pin))
        {
            return null;
        }

        if (!settings.SelfHosted || string.IsNullOrWhiteSpace(path) || string.IsNullOrWhiteSpace(pin))
        {
            throw new InvalidOperationException("Operator-owned license trust requires self-hosting, a public certificate path and a SHA-256 pin.");
        }

        pin = pin.Replace(":", string.Empty).Replace(" ", string.Empty);
        if (!Regex.IsMatch(pin, "\\A[0-9a-fA-F]{64}\\z"))
        {
            throw new InvalidOperationException("Operator-owned license certificate pin must be a SHA-256 fingerprint.");
        }

        var certificate = CoreHelpers.GetCertificate(path, string.Empty);
        try
        {
            if (certificate.HasPrivateKey)
            {
                throw new InvalidOperationException("The server license verification certificate must not contain a private key.");
            }

            using var rsa = certificate.GetRSAPublicKey();
            if (rsa is null || rsa.KeySize < 2048)
            {
                throw new InvalidOperationException("The license verification certificate requires an RSA key of at least 2048 bits.");
            }

            if (!certificate.GetCertHashString(HashAlgorithmName.SHA256).Equals(pin, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException("Operator-owned license certificate does not match its SHA-256 pin.");
            }

            var now = DateTime.UtcNow;
            if (certificate.NotBefore.ToUniversalTime() > now || certificate.NotAfter.ToUniversalTime() <= now)
            {
                throw new InvalidOperationException("Operator-owned license verification certificate is outside its validity period.");
            }

            return certificate;
        }
        catch
        {
            certificate.Dispose();
            throw;
        }
    }
}
