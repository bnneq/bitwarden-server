using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text.Json;
using Bit.Core.AdminConsole.Entities;
using Bit.Core.Billing.Licenses.Services;
using Bit.Core.Billing.Licenses.Services.Implementations;
using Bit.Core.Billing.Models.Business;
using Bit.Core.Billing.Services;
using Bit.Core.Entities;
using Bit.Core.Platform.Push;
using Bit.Core.Repositories;
using Bit.Core.Services;
using Bit.Core.Settings;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.IdentityModel.Tokens;
using NSubstitute;
using Xunit;

namespace Bit.Core.Test.Billing.Services;

public sealed class SelfHostedLicenseCertificateTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), $"local-licensing-{Guid.NewGuid():N}");
    private readonly X509Certificate2 _certificate;
    private readonly GlobalSettings _settings;

    public SelfHostedLicenseCertificateTests()
    {
        Directory.CreateDirectory(_directory);
        using var rsa = RSA.Create(2048);
        var request = new CertificateRequest("CN=Local License Test", rsa,
            HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
        _certificate = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-1), DateTimeOffset.UtcNow.AddYears(2));
        var path = Path.Combine(_directory, "issuer.cer");
        File.WriteAllBytes(path, _certificate.Export(X509ContentType.Cert));
        _settings = new GlobalSettings
        {
            SelfHosted = true,
            LicenseDirectory = _directory,
            SelfHostedLicenseCertificatePath = path,
            SelfHostedLicenseCertificateSha256 = _certificate.GetCertHashString(HashAlgorithmName.SHA256),
        };
    }

    [Fact]
    public void NoConfiguration_PreservesDefaultTrust()
    {
        Assert.Null(SelfHostedLicenseCertificate.Load(new GlobalSettings()));
    }

    [Fact]
    public void MatchingPin_LoadsPublicCertificate()
    {
        using var loaded = SelfHostedLicenseCertificate.Load(_settings);
        Assert.NotNull(loaded);
        Assert.False(loaded.HasPrivateKey);
        Assert.Equal(_certificate.Thumbprint, loaded.Thumbprint);
    }

    [Fact]
    public void CloudConfiguration_IsRejected()
    {
        _settings.SelfHosted = false;
        Assert.Throws<InvalidOperationException>(() => SelfHostedLicenseCertificate.Load(_settings));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("not-a-fingerprint")]
    [InlineData("0000000000000000000000000000000000000000000000000000000000000000")]
    public void MissingInvalidOrMismatchedPin_IsRejected(string? pin)
    {
        _settings.SelfHostedLicenseCertificateSha256 = pin;
        Assert.Throws<InvalidOperationException>(() => SelfHostedLicenseCertificate.Load(_settings));
    }

    [Fact]
    public void PrivateKeyOnServer_IsRejected()
    {
        File.WriteAllBytes(_settings.SelfHostedLicenseCertificatePath, _certificate.Export(X509ContentType.Pfx));
        Assert.Throws<InvalidOperationException>(() => SelfHostedLicenseCertificate.Load(_settings));
    }

    [Fact]
    public void ExpiredCertificate_IsRejected()
    {
        using var rsa = RSA.Create(2048);
        var request = new CertificateRequest("CN=Expired License Test", rsa,
            HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
        using var expired = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddDays(-2), DateTimeOffset.UtcNow.AddDays(-1));
        File.WriteAllBytes(_settings.SelfHostedLicenseCertificatePath, expired.Export(X509ContentType.Cert));
        _settings.SelfHostedLicenseCertificateSha256 = expired.GetCertHashString(HashAlgorithmName.SHA256);
        Assert.Throws<InvalidOperationException>(() => SelfHostedLicenseCertificate.Load(_settings));
    }

    [Fact]
    public void Production_VerifiesOperatorSignedLicense()
    {
        Assert.True(CreateSut().VerifyLicense(NewLicense()));
    }

    [Fact]
    public void Production_RejectsExpiredToken()
    {
        Assert.False(CreateSut().VerifyLicense(NewLicense(expired: true)));
    }

    [Fact]
    public void Production_RejectsChangedAudience()
    {
        var license = NewLicense();
        license.Id = Guid.NewGuid();
        Assert.False(CreateSut().VerifyLicense(license));
    }

    [Fact]
    public void Production_RejectsTamperedPayload()
    {
        var license = NewLicense();
        var parts = license.Token.Split('.');
        parts[1] = Base64UrlEncoder.Encode("{\"Premium\":\"True\"}");
        license.Token = string.Join('.', parts);
        Assert.False(CreateSut().VerifyLicense(license));
    }

    [Fact]
    public void DefaultProduction_RejectsOperatorCertificate()
    {
        _settings.SelfHostedLicenseCertificatePath = null;
        _settings.SelfHostedLicenseCertificateSha256 = null;
        Assert.False(CreateSut().VerifyLicense(NewLicense()));
    }

    [Fact]
    public async Task Production_UserValidationAcceptsMatchingSignedLicense()
    {
        var license = NewLicense();
        Directory.CreateDirectory(Path.Combine(_directory, "user"));
        await File.WriteAllTextAsync(Path.Combine(_directory, "user", $"{license.Id}.json"), JsonSerializer.Serialize(license));
        var user = new User { Id = license.Id, Email = license.Email, LicenseKey = license.LicenseKey, Premium = true };
        Assert.True(await CreateSut().ValidateUserPremiumAsync(user));
    }

    private LicensingService CreateSut()
    {
        var environment = Substitute.For<IWebHostEnvironment>();
        environment.EnvironmentName = Environments.Production;
        return new LicensingService(Substitute.For<IUserRepository>(), Substitute.For<IOrganizationRepository>(),
            Substitute.For<IMailService>(), environment, NullLogger<LicensingService>.Instance, _settings,
            Substitute.For<ILicenseClaimsFactory<Organization>>(), new UserLicenseClaimsFactory(),
            Substitute.For<IPushNotificationService>());
    }

    private UserLicense NewLicense(bool expired = false)
    {
        var license = new UserLicense
        {
            Id = Guid.NewGuid(),
            Email = "local@example.com",
            LicenseKey = Guid.NewGuid().ToString(),
            Premium = true,
        };
        var token = new JwtSecurityToken("bitwarden", $"user:{license.Id}",
            [new Claim("Email", license.Email), new Claim("LicenseKey", license.LicenseKey), new Claim("Premium", "True")],
            DateTime.UtcNow.AddHours(-1), expired ? DateTime.UtcNow.AddMinutes(-1) : DateTime.UtcNow.AddDays(1),
            new SigningCredentials(new X509SecurityKey(_certificate), SecurityAlgorithms.RsaSha256));
        license.Token = new JwtSecurityTokenHandler().WriteToken(token);
        return license;
    }

    public void Dispose()
    {
        _certificate.Dispose();
        Directory.Delete(_directory, recursive: true);
    }
}
