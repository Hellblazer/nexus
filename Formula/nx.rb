class Nx < Formula
  desc "Self-hosted semantic search and knowledge management CLI"
  homepage "https://github.com/Hellblazer/nexus"
  url "https://files.pythonhosted.org/packages/06/c0/262786ae7821c47d54749f32964fcc1187960cfd61bfe5580a1e27e7ed6f/nexus-0.3.1.tar.gz"
  sha256 "e46e44a59896d0d5a95749cfcae060f3cd1aa8db7ea43a0ea3c2fcf0d80fe503"
  license "AGPL-3.0-or-later"

  depends_on "uv" => :build
  depends_on "python@3.12"

  def install
    venv = libexec/"venv"
    system "uv", "venv", "--python", Formula["python@3.12"].opt_bin/"python3.12", venv.to_s
    system "uv", "pip", "install",
           "--python", (venv/"bin/python").to_s,
           "nexus==#{version}"
    bin.install_symlink venv/"bin/nx"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/nx --version")
  end
end
